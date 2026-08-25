# -*- coding: utf-8 -*-
"""V4.6 实验：SC 多采样 + Code-First 投票（对 round4 仍错 47 题）。

设计（AIMO2 银牌路线 + STAR-PólyaMath 双扰动）：
- 3 视角推理采样：温度 0.7/1.0/1.3 + 方法提示扰动（代数/构造/枚举）
- 3 份独立代码求解：不同算法提示，subprocess 执行（本地实验用）
- 投票：闭式等价判定（rule_judge），≥2/3 一致才锁定；早停（第2份一致即停）
- 评估：与官方答案比对（仅本地实验，生产管线不读答案）

用法：python local_test/sc_experiment.py [--subset 47] [--max-questions N]
"""
import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.llm_retry import chat_prefilled, thinking_mode_flag
from utils.deps import get_deps
from utils.cot_stripper import strip_cot_prefix
from nodes.reasoning_agent_node import _parse_reasoning_output
from utils.rule_judge import (
    is_closed_form_answer, answers_numerically_equivalent)
from config import CONFIG

# 方法扰动提示（3 视角）
_VIEW_HINTS = {
    "algebra": "\n求解视角：代数法——直接建立方程/不等式/递推关系，符号推导求解。",
    "construct": "\n求解视角：构造法——构造具体对象/反例/极端情形，先验证小规模再推广。",
    "enumerate": "\n求解视角：枚举法——小规模暴力枚举找规律，归纳通项或直接计数。",
}
_TEMPS = {"algebra": 0.7, "construct": 1.0, "enumerate": 1.3}

_CODE_HINTS = {
    "brute": "用暴力枚举/搜索（itertools/for 循环）直接计算，小规模验证后再放大。",
    "sympy": "用 sympy 符号计算（积分/方程/化简/矩阵），保持精确闭式。",
    "dp": "用动态规划/递推（自底向上填表），注意边界与初值。",
}

_REASON_BASE = """你是一个数学专家。严格按以下格式解答（**最终答案必须写在最前面**）：

## 最终答案
[明确的最终结果]

## 问题分析
[分析]

## 详细解题步骤
步骤1：...
计算过程：...

{view_hint}

问题：
{problem}
"""

_CODE_BASE = """请为下面的数学题写一个完整可运行的 Python 程序求解（{code_hint}）。
最后一行必须 print("最终答案:", answer)，answer 是精确值（分数/根式/整数）。
只输出 ```python``` 代码块。

问题：
{problem}
"""


def _load_questions(max_q=None):
    """从 retest graded 取仍错题（含题面）。"""
    g = json.load(open("local_test/results/set1_round_004_retest_v45_graded.json",
                       encoding="utf-8"))
    probs = g.get("problems", g) if isinstance(g, dict) else g
    rows = {}
    for line in open("local_test/data/schedule/set1_round_004.jsonl",
                     encoding="utf-8"):
        line = line.strip()
        if line:
            d = json.loads(line)
            rows[d["idx"]] = d
    qs = []
    for p in probs:
        if p.get("llm_verdict") == "correct":
            continue
        r = rows.get(p.get("idx"))
        if r:
            qs.append({"idx": p.get("idx"), "problem": r.get("problem", "")})
    qs.sort(key=lambda x: x["idx"])
    if max_q:
        qs = qs[:max_q]
    return qs


def _extract_code(resp):
    m = re.search(r"```python[ \t]*\n(.*?)\n```", resp or "", re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*\n?(.*?)\n?```", resp or "", re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


def _run_code(code, timeout=60):
    """本地 subprocess 执行（实验用；生产走 MCP 沙箱）。"""
    try:
        r = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            timeout=timeout, cwd=str(Path(__file__).resolve().parent.parent))
        return r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT"
    except Exception as exc:  # noqa: BLE001
        return "", str(exc)[:200]


def _extract_code_answer(stdout):
    m = re.search(r"最终答案[：:]\s*(.+)", stdout or "", re.MULTILINE)
    if m:
        return m.group(1).strip()
    # 最后一行的值
    lines = [l.strip() for l in (stdout or "").splitlines() if l.strip()]
    for l in reversed(lines):
        if re.fullmatch(r"[-+0-9/\\a-zA-Z\s\^\(\)\.\*\√π,_]+", l) and any(
                c.isdigit() for c in l):
            return l
    return ""


def _text_equiv(a, b):
    """文本等价：去空白/标点后一致（文本答案投票通道）。"""
    if not a or not b:
        return False
    ka = re.sub(r"[\s。．.,，;；:：!！?？\"'“”‘’()（）\[\]【】\-_]+", "", str(a))
    kb = re.sub(r"[\s。．.,，;；:：!！?？\"'“”‘’()（）\[\]【】\-_]+", "", str(b))
    return bool(ka) and ka.lower() == kb.lower() and len(ka) >= 3


def _same_answer(a, b):
    """闭式等价或文本等价任一通过即视为同一答案。"""
    if is_closed_form_answer(a) and is_closed_form_answer(b):
        eq = answers_numerically_equivalent(a, b)
        if eq is not None:
            return eq
    return _text_equiv(a, b)


def solve_one(idx, problem, main_map, budget_seconds=1200):
    from utils.deps import Deps
    from llm_client import InternChatClient
    from utils.time_budget import TimeBudget
    import logging
    client = InternChatClient()
    deps = Deps(client=client,
                time_budget=TimeBudget(budget_seconds),
                logger=logging.getLogger("sc_experiment"))
    q = {"idx": idx, "problem": problem}
    official = main_map.get(problem.strip(), "?")
    out = {"idx": idx, "official": official[:80],
           "reasoning": {}, "code": {}}

    # 3 视角推理采样（早停：第 2 份与第 1 份闭式等价即停）
    r_answers = []
    for view in ("algebra", "construct", "enumerate"):
        prompt = _REASON_BASE.format(problem=problem[:6000],
                                     view_hint=_VIEW_HINTS[view])
        try:
            resp = chat_prefilled(
                deps.client,
                messages=[{"role": "user", "content": prompt}],
                prefix="## 最终答案\n",
                temperature=_TEMPS[view],
                max_tokens=8192,
                logger=deps.logger,
                time_budget=deps.time_budget,
                label=f"sc_reason_{view}",
                thinking_mode=thinking_mode_flag(),
            )
        except Exception as exc:  # noqa: BLE001
            out["reasoning"][view] = {"answer": "", "error": str(exc)[:80]}
            continue
        parsed = _parse_reasoning_output(resp or "")
        ans = parsed.get("answer") or ""
        out["reasoning"][view] = {"answer": ans[:120]}
        if ans:
            r_answers.append(ans)
            if len(r_answers) >= 2 and _same_answer(r_answers[0], r_answers[1]):
                break  # 早停

    # 3 份独立代码（早停：两代码结果等价即停）
    c_answers = []
    for algo in ("brute", "sympy", "dp"):
        prompt = _CODE_BASE.format(problem=problem[:6000],
                                   code_hint=_CODE_HINTS[algo])
        try:
            resp = chat_prefilled(
                deps.client,
                messages=[{"role": "user", "content": prompt}],
                prefix="```python\n",
                temperature=0.4,
                max_tokens=8192,
                logger=deps.logger,
                time_budget=deps.time_budget,
                label=f"sc_code_{algo}",
                thinking_mode=thinking_mode_flag(),
            )
        except Exception as exc:  # noqa: BLE001
            out["code"][algo] = {"answer": "", "error": str(exc)[:80]}
            continue
        code = _extract_code(resp or "")
        if not code:
            out["code"][algo] = {"answer": "", "error": "no code"}
            continue
        stdout, stderr = _run_code(code)
        ans = _extract_code_answer(stdout)
        out["code"][algo] = {"answer": ans[:120],
                             "error": stderr[:80] if not ans else ""}
        if ans:
            c_answers.append(ans)
            if len(c_answers) >= 2 and _same_answer(c_answers[0], c_answers[1]):
                break  # 早停

    # 投票判定（闭式/文本双通道）
    out["r_votes"] = {a: r_answers.count(a) for a in set(r_answers)}
    out["c_votes"] = {a: c_answers.count(a) for a in set(c_answers)}
    out["r_answers"] = r_answers[:3]
    out["c_answers"] = c_answers[:3]
    # 与官方比对（闭式等价或文本等价）
    out["r_correct"] = any(
        _same_answer(a, official) for a in r_answers)
    out["c_correct"] = any(
        _same_answer(a, official) for a in c_answers)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-questions", type=int, default=0)
    args = ap.parse_args()

    from utils.deps import Deps
    from llm_client import InternChatClient
    from utils.time_budget import TimeBudget
    import logging
    client = InternChatClient()
    deps = Deps(client=client,
                time_budget=TimeBudget(1200),
                logger=logging.getLogger("sc_experiment"))
    main_map = {}
    with open("local_test/data/omni_math/test.jsonl", encoding="utf-8") as f:
        for i, line in enumerate(f):
            d = json.loads(line)
            main_map[d["problem"].strip()] = d["answer"]

    qs = _load_questions(args.max_questions)
    print(f"实验题数: {len(qs)}")
    results = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(solve_one, q["idx"], q["problem"], main_map): q["idx"]
                for q in qs}
        done = 0
        for fut in as_completed(futs):
            done += 1
            idx = futs[fut]
            try:
                r = fut.result()
                results.append(r)
                mark = "✓" if (r["r_correct"] or r["c_correct"]) else "✗"
                print(f"[{done}/{len(qs)}] #{idx} {mark} "
                      f"r={'/'.join(a[:28] for a in r['r_answers'])} "
                      f"c={'/'.join(a[:28] for a in r['c_answers'])}")
            except Exception as exc:  # noqa: BLE001
                print(f"[{done}/{len(qs)}] #{idx} ERROR {exc}")

    r_saved = sum(1 for r in results if r["r_correct"])
    c_saved = sum(1 for r in results if r["c_correct"])
    both = sum(1 for r in results if r["r_correct"] and r["c_correct"])
    print("\n=== V4.6 SC 实验结论 ===")
    print(f"3视角推理救回: {r_saved}/{len(results)}")
    print(f"3代码求解救回: {c_saved}/{len(results)}")
    print(f"两者都救: {both}")
    print(f"并集救回: {sum(1 for r in results if r['r_correct'] or r['c_correct'])}/{len(results)}")
    print(f"耗时: {(time.monotonic()-t0)/60:.1f} min")
    out_path = "local_test/results/sc_experiment.json"
    Path(out_path).write_text(json.dumps(results, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"结果: {out_path}")


if __name__ == "__main__":
    main()
