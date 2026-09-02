"""本地评测：用真实 LLM 接口跑测试集并对照参考答案判分。

用法（在项目根目录）：
    python eval_local.py                 # 默认跑全部 50 题，并发 1
    python eval_local.py --limit 5       # 只跑前 5 题，快速验证方向
    python eval_local.py --idx 0,3,7     # 只跑指定 idx（逗号分隔）
    python eval_local.py --concurrency 2 # 并发求解（共享同一个 client）
    python eval_local.py --out out.json  # 结果写到哪里（默认 eval_results.json）

密钥从 .env 读取：INTERN_API_KEY 必填；INTERN_API_URL / INTERN_MODEL 可选。
判分口径：
    * 计算题：从 final_response 抠出答案后走 AnswerCompare 符号/数值等价
      （容差 1e-6），这是平台判分同思路的本地近似，不是平台本身；
    * 证明题：同样走 AnswerCompare，匹配不上即记错——本地只做粗略对照。
"""
import argparse
import json
import os
import sys
import time
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_env(path: Path = ROOT / ".env") -> dict:
    """读取 .env：INTERN_API_KEY / INTERN_API_URL / INTERN_MODEL。"""
    env = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


class LocalClient:
    """OpenAI 兼容 Chat 接口的最小客户端，仅实现 pipeline 需要的 chat()。

    pipeline 通过 `client.chat(messages=..., temperature=..., max_tokens=...)`
    调用；这里把请求落到 Intern 平台，返回纯文本 assistant 内容。
    """

    def __init__(self, env: dict):
        self.api_key = env.get("INTERN_API_KEY", "")
        self.url = env.get(
            "INTERN_API_BASE",
            env.get("INTERN_API_URL",
                   "https://chat.intern-ai.org.cn/api/v1/chat/completions"),
        )
        self.model = env.get("INTERN_MODEL", "intern-s2-preview-397b")
        if not self.api_key:
            raise SystemExit("缺少 INTERN_API_KEY，请先按 .env.example 填写 .env")

    def chat(self, messages, temperature=0.2, max_tokens=4096):
        import requests

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # 读超时放到 1300s：让 pipeline 自己的墙钟预算（1200s）来决定何时放弃，
        # 传输层不该在难题的正常长推理中途切断（曾把 >600s 的调用掐掉导致降级）。
        resp = requests.post(
            self.url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
            timeout=1300,
        )
        resp.raise_for_status()
        data = resp.json()
        # 有些网关把内容包在 choices[0].message.content，有些是 delta/多段。
        content = data["choices"][0]["message"].get("content", "")
        return content if isinstance(content, str) else "".join(
            seg.get("text", "") if isinstance(seg, dict) else str(seg)
            for seg in content
        )


def extract_answer(final_response: str) -> str:
    """从 final_response 抠出「最终答案」段或前缀标记之后的文本。

    四章节输出里答案在 `## 最终答案` 小节；散文式输出带 `最终答案：` 前缀。
    都找不到时返回整段原文，交给等价判定去处理。
    """
    text = (final_response or "").strip()
    if not text:
        return ""
    # 优先按 Markdown 小节切：取「最终答案」小节，到下一个 `## ` 为止。
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^#{2,4}\s*(最终答案|结论)\s*[：:]?\s*$", line.strip()):
            body = []
            for j in range(i + 1, len(lines)):
                if re.match(r"^##+\s", lines[j].strip()):
                    break
                body.append(lines[j])
            return "\n".join(body).strip()
    # 散文式：剥前缀标记。
    for prefix in ("最终答案：", "最终答案:", "结论：", "结论:", "答案："):
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def grade(problem: str, final_response: str, expected: str) -> tuple:
    """判分单题。返回 (ok, reason)。

    统一走 AnswerCompare.match_computation_answer：它能处理数值、符号表达式、
    分数、矩阵与复合答案；证明题的字符串答案也走同一条管道，匹配不上算错。
    """
    from utils.answer_compare import AnswerCompare

    pred = extract_answer(final_response)
    if not pred:
        return False, "no_answer"
    try:
        ok, _conf, reason = AnswerCompare.match_computation_answer(pred, expected)
        return bool(ok), reason
    except Exception as exc:  # noqa: BLE001 - 判分器偶发失败不应中断整批。
        return False, f"grader_error: {exc}"


def run_one(idx: int, problem: str, expected: str, agent) -> dict:
    """求解单题并判分。单题超时由 pipeline 自带的 TimeBudget(1200s) 约束。"""
    started = time.time()
    try:
        result = agent.solve(problem, {"idx": idx})
    except Exception as exc:  # noqa: BLE001 - solve 理论上不抛，这里兜底记录。
        return {"idx": idx, "ok": False, "reason": f"solve_error: {exc}",
                "elapsed_s": round(time.time() - started, 1),
                "predicted": "", "expected": expected}
    predicted = result.get("final_response", "")
    ok, reason = grade(problem, predicted, expected)
    return {
        "idx": idx, "ok": ok, "reason": reason,
        "elapsed_s": round(time.time() - started, 1),
        "predicted": predicted[:400], "expected": expected[:400],
        "trace_steps": len(result.get("trace", [])),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=r"E:/math/测试/测试集08_模拟50.jsonl")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题")
    parser.add_argument("--idx", default="", help="逗号分隔的题号，覆盖 --limit")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--out", default="eval_results.json")
    args = parser.parse_args()

    env = load_env()
    client = LocalClient(env)
    from user_agent import ReasoningAgent

    agent = ReasoningAgent(client)

    rows = [json.loads(line) for line in open(args.dataset, encoding="utf-8")]
    if args.idx:
        wanted = {int(x) for x in args.idx.split(",") if x.strip()}
        rows = [r for r in rows if r.get("idx") in wanted or r.get("idx", -1) in wanted]
    elif args.limit:
        rows = rows[: args.limit]
    print(f"评测 {len(rows)} 题，并发 {args.concurrency}，模型 "
          f"{client.model}", flush=True)

    # 断点续跑：已存在的结果文件里跳过已完成的 idx。
    out_path = Path(args.out)
    done = {}
    if out_path.exists():
        done = {str(r["idx"]): r for r in json.loads(out_path.read_text(encoding="utf-8"))}
    todo = [r for r in rows if str(r.get("idx", -1)) not in done]

    def work(row):
        idx = row.get("idx", -1)
        problem = row.get("problem", "")
        expected = row.get("answer", "")
        rec = run_one(idx, problem, expected, agent)
        print(f"[{'OK' if rec['ok'] else 'X '}] idx={idx} "
              f"{rec['elapsed_s']}s  {rec['reason']}", flush=True)
        done[str(idx)] = rec
        out_path.write_text(json.dumps(list(done.values()), ensure_ascii=False, indent=1),
                            encoding="utf-8")
        return rec

    if args.concurrency > 1:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            list(pool.map(work, todo))
    else:
        for row in todo:
            work(row)

    results = list(done.values())
    ok = sum(1 for r in results if r.get("ok"))
    total = len(results)
    print(f"\n完成 {total} 题，正确 {ok} 题，acc = {ok / total:.1%}" if total else "\n无新增题")
    print(f"结果写入 {out_path}")


if __name__ == "__main__":
    main()
