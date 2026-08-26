# -*- coding: utf-8 -*-
"""本地批量求题测试脚本：跑题集 → 采集指标 → 生成 results.json（前端监控面板数据源）。

用法:
    # 默认跑 local_test/data/dev.jsonl，结果写 local_test/results/results.json
    python local_test/run_tests.py

    # 指定题集 / 输出 / 并发
    python local_test/run_tests.py --input sample_data/dev.jsonl --output results/run1.json --concurrency 4

采集指标（全部来自求解后 final_state，零侵入）：
    - difficulty: easy/medium/hard（分类节点顺带输出）
    - elapsed_s: 单题墙钟耗时
    - api_calls: LLM 调用次数 + 总耗时（_llm_spend_log，TimeBudget.record 单点收口）
    - node_timings: 各阶段实测耗时
    - correct: 与标准答案比对（复用 utils.answer_matcher.match_computation_answer，
      符号等价判定，非字符串精确匹配；无 answer 字段时标记 unknown）
    - trace: 完整推理过程（前端展开查看步骤/证据）
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

# 保证可以从项目根目录或任意 cwd 运行
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# V5.3 审计修复配套：本地运行注入规则可判定文件路径（官方环境无此变量，
# 判定器优雅降级行为不变）。路径：local_test/data/omni_math/omni_math_rule.jsonl
import os  # noqa: E402
if not os.environ.get("OMNI_RULE_PATH"):
    _rule_f = _HERE / "data" / "omni_math" / "omni_math_rule.jsonl"
    if _rule_f.exists():
        os.environ["OMNI_RULE_PATH"] = str(_rule_f)

from utils.logger import get_logger  # noqa: E402

logger = get_logger("local_test")


def load_jsonl(path: Path):
    """加载 JSONL 题集；兼容 dict 或 list 两种输入文件形态。"""
    items = []
    if path.suffix == ".jsonl":
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    items.append(json.loads(line))
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else [data]
    # 归一化：保证 idx
    for i, item in enumerate(items):
        item.setdefault("idx", i)
    return items


def grade(expected, candidate, problem=""):
    """本地不再做任何判分（2026-08-11 用户约定）。

    评分统一由 local_test/grade_llm.py 用 Intern-S 模型判定（与官方 Judger
    同口径：模型综合判断，不做字符串/符号精确匹配）。本函数始终返回 None
    （未判分），求解脚本只负责采集结果，判分在评分阶段完成。
    """
    return None


def summarize(results):
    """汇总统计：正确率 / 难度分布 / API 总量 / 耗时。"""
    total = len(results)
    graded = [r for r in results if r["correct"] is not None]
    correct = sum(1 for r in graded if r["correct"])
    by_difficulty = {}
    for r in results:
        d = r.get("difficulty") or "unknown"
        slot = by_difficulty.setdefault(d, {"total": 0, "correct": 0, "graded": 0})
        slot["total"] += 1
        if r["correct"] is not None:
            slot["graded"] += 1
            if r["correct"]:
                slot["correct"] += 1
    return {
        "total": total,
        "graded": len(graded),
        "correct": correct,
        "accuracy": round(correct / len(graded), 4) if graded else 0.0,
        "total_elapsed_s": round(sum(r["elapsed_s"] for r in results), 2),
        "avg_elapsed_s": round(sum(r["elapsed_s"] for r in results) / total, 2) if total else 0.0,
        "total_api_calls": sum(r["api_calls"] for r in results),
        "total_api_seconds": round(sum(r["api_seconds"] for r in results), 2),
        "by_difficulty": {
            d: {
                "total": v["total"],
                "graded": v["graded"],
                "correct": v["correct"],
                "accuracy": round(v["correct"] / v["graded"], 4) if v["graded"] else 0.0,
            }
            for d, v in sorted(by_difficulty.items())
        },
    }


def solve_one(agent_factory, item, idx):
    """单题求解 + 指标采集。agent_factory 延迟创建避免并发共享状态。"""
    start = time.monotonic()
    agent = agent_factory()
    problem = str(item.get("problem") or "")
    metadata = {"idx": idx}
    for key in ("subject", "question_mode", "question_type", "category",
                "rule_judgeable", "difficulty"):
        if key in item and item[key] not in (None, ""):
            metadata[key] = item[key]

    record = {
        "idx": idx,
        "problem": problem[:2000],
        "expected": item.get("answer"),
        "final_response": "",
        "difficulty": "",
        "correct": None,
        "elapsed_s": 0.0,
        "api_calls": 0,
        "api_seconds": 0.0,
        "node_timings": [],
        "trace": [],
        "status": "success",
        "error": "",
    }
    try:
        result = agent.solve(problem, metadata)
        record["final_response"] = str(result.get("final_response") or "")
        record["trace"] = result.get("trace", [])
        # 指标从 trace 提取（solve 返回的 trace 已带 timing/classification 步骤）：
        #   - timing 步骤: node_elapsed(各阶段耗时) + llm_calls(API 调用明细)
        #   - classification 步骤: difficulty(easy/medium/hard)
        for entry in record["trace"]:
            step = entry.get("step")
            if step == "timing":
                tools = entry.get("tools") or []
                for t in tools:
                    if t.get("name") == "time_budget":
                        record["node_timings"] = t.get("node_elapsed", [])
                        spend = t.get("llm_calls") or []
                        record["api_calls"] = len(spend)
                        record["api_seconds"] = round(
                            sum(s.get("seconds", 0.0) for s in spend), 2)
            elif step == "classification":
                if not record["difficulty"]:
                    record["difficulty"] = entry.get("difficulty") or ""
        record["correct"] = grade(item.get("answer"), record["final_response"], problem)
    except Exception as exc:  # noqa: BLE001 - 单题失败不拖垮整批
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["elapsed_s"] = round(time.monotonic() - start, 2)
    return record


def _write_payload(items_done, input_file, output, total_planned=0, elapsed_s=0.0):
    """把已完成题目写成 results.json（面板数据源）。

    `items_done` 为已完成题目的 record 列表（与输入 idx 一一对应）。
    增量模式：只写已完成的部分，summary 基于已完成题统计——面板即可实时
    看到"已跑 N/M、当前正确率"，无需等全量结束。
    `total_planned` 为计划总题数（前端显示进度 N/M 用）。
    `elapsed_s` 为脚本已运行秒数（心跳刷新时保持递增，前端显示运行时长）。
    """
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "input_file": str(Path(input_file)),
        "running": True,
        "total_planned": total_planned,
        "wall_elapsed_s": round(elapsed_s, 1),
        "summary": summarize(items_done),
        "problems": items_done,
    }
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # latest.json：server.py 优先读它（增量刷新），results.json 由主流程收尾覆盖
    try:
        latest = out.parent / "latest.json"
        latest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def main():
    parser = argparse.ArgumentParser(description="本地批量求题测试")
    parser.add_argument("--input", default=str(_HERE / "data" / "dev.jsonl"))
    parser.add_argument("--output", default=str(_HERE / "results" / "results.json"))
    parser.add_argument("--concurrency", type=int, default=1,
                        help="并发数（默认 1；>1 时用线程池）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0=全部）")
    parser.add_argument("--start-idx", type=int, default=0, help="从第 start-idx 题开始（断点续跑）")
    parser.add_argument("--mock", action="store_true",
                        help="无 INTERN_API_KEY 时用模拟 client 验证链路（不出真答案）")
    parser.add_argument("--live", action="store_true",
                        help="实时增量写入 results/latest.json，面板秒级刷新进度")
    parser.add_argument("--resume", default="",
                        help="已有结果文件路径：跳过其中已成功（final_response 非兜底文案）的题，只重跑失败的题")
    args = parser.parse_args()

    items = load_jsonl(Path(args.input))
    if args.limit > 0:
        items = items[: args.limit]
    if args.start_idx > 0:
        items = items[args.start_idx:]
    if not items:
        logger.error("题集为空: %s", args.input)
        sys.exit(1)

    # 断点续跑：合并已有成功结果，只重跑失败的题
    carried = {}
    if args.resume:
        rp = Path(args.resume)
        if rp.exists():
            prev = json.loads(rp.read_text(encoding="utf-8"))
            for p in prev.get("problems", []):
                fr = str(p.get("final_response") or "")
                # 成功判定：final_response 非空且不是兜底文案
                if fr.strip() and "解题过程中出现错误" not in fr:
                    carried[p["idx"]] = p
            logger.info("断点续跑: 已有 %d 题成功结果将被保留，只重跑其余题目",
                        len(carried))
        else:
            logger.warning("--resume 文件不存在: %s（将全量重跑）", args.resume)

    # 过滤出需要重跑的题
    todo = [it for it in items if it["idx"] not in carried]
    logger.info("开始求解 %d 题（并发=%d，其中续跑保留 %d 题）...",
                len(todo), args.concurrency, len(carried))

    def factory():
        from user_agent import ReasoningAgent
        from llm_client import InternChatClient
        try:
            client = InternChatClient()
        except RuntimeError:
            if not args.mock:
                logger.error("未检测到 INTERN_API_KEY。真机测试请先设置环境变量；"
                             "仅验证链路可加 --mock。")
                sys.exit(2)
            client = _MockClient()
        return ReasoningAgent(client=client)

    results = []
    lock = __import__("threading").Lock()
    total_planned = len(items)
    start_wall = time.monotonic()

    # 启动即写初始进度（0/N，running=true），面板立刻显示"开始运行"
    _write_payload([], args.input, args.output, total_planned=total_planned,
                   elapsed_s=0.0)

    def _heartbeat():
        """心跳：每 15s 刷新一次进度文件，让面板时间戳持续跳动（证明在运行）。"""
        while True:
            time.sleep(15)
            with lock:
                _write_payload(sorted(results, key=lambda r: r["idx"]),
                               args.input, args.output, total_planned=total_planned,
                               elapsed_s=time.monotonic() - start_wall)

    if args.live:
        import threading as _t
        _t.Thread(target=_heartbeat, daemon=True).start()

    def on_done(record):
        """单题完成回调：并发安全地追加 + 实时写盘。"""
        with lock:
            results.append(record)
            done = len(results)
            merged = results + list(carried.values())
            if args.live or done % 5 == 0 or done == len(todo):
                _write_payload(sorted(merged, key=lambda r: r["idx"]),
                               args.input, args.output, total_planned=total_planned,
                               elapsed_s=time.monotonic() - start_wall)
            logger.info("[%d/%d] idx=%s 完成, 正确=%s, 耗时%.1fs, API %d次",
                        done, len(todo), record["idx"], record["correct"],
                        record["elapsed_s"], record["api_calls"])

    if args.concurrency > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(solve_one, factory, it, it["idx"]) for it in todo]
            for f in futures:
                on_done(f.result())
    else:
        for it in todo:
            on_done(solve_one(factory, it, it["idx"]))

    # 合并断点续跑保留的成功结果
    for idx, p in carried.items():
        results.append(p)

    results.sort(key=lambda r: r["idx"])
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "input_file": str(Path(args.input)),
        "running": False,
        "total_planned": total_planned,
        "summary": summarize(results),
        "problems": results,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # 收尾：latest.json 与 results.json 同步（面板可继续读 latest.json）
    try:
        latest = out.parent / "latest.json"
        latest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    s = payload["summary"]
    logger.info("完成: %d 题, 判分 %d, 正确 %d, 正确率 %.1f%%, 总耗时 %.1fs, API %d 次",
                s["total"], s["graded"], s["correct"], s["accuracy"] * 100,
                s["total_elapsed_s"], s["total_api_calls"])
    print(f"\n结果已写入: {out}")


class _MockClient:
    """无 API Key 时的链路验证 client。

    按提示词形态返回不同响应，让整条图链路（分类→求解→验证→仲裁→成稿）
    真实跑通，便于前端面板演示与数据流调试：
      - 分类调用（含"可选类别"）：返回 category + difficulty（按题号轮换难度）
      - 求解/验证调用：返回四章节 JSON（analysis/steps/answer/validation_points）
    """

    def chat(self, messages=None, temperature=0.2, max_tokens=4096, **kwargs):
        prompt = ""
        if isinstance(messages, list):
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "user":
                    prompt += str(m.get("content") or "")
        if "可选类别" in prompt:
            idx = abs(hash(prompt)) % 3
            cat_map = {0: "抽象代数", 1: "数学分析", 2: "离散数学"}
            diff_map = {0: "hard", 1: "medium", 2: "easy"}
            return '{"category": "%s", "confidence": 0.92, "difficulty": "%s"}' % (
                cat_map[idx], diff_map[idx])
        return (
            "{\"analysis\":\"（模拟）由分类与求解链路生成\","
            "\"steps\":[\"步骤1：建模\",\"步骤2：计算\",\"步骤3：验算\"],"
            "\"answer\":\"42\",\"validation_points\":[\"边界检查通过\"]}"
        )


if __name__ == "__main__":
    main()
