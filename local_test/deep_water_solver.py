# -*- coding: utf-8 -*-
"""深水区专项攻坚求解器（V4.9 SVC：采样-验证-确认）。

对 47 道深水区题（多轮重测仍错）做最大火力攻坚：
1. 采样：6 视角推理（竞赛方法增强）+ 3 份代码 = 9 份候选
2. 筛选：去重计票 → top-2 候选
3. 验证：对 top-2 做双独立程序化验证（代入/枚举/反例搜索）
4. 确认：验证通过 → 确定性锁定；否则 2/3 投票 → 弱锁；都不行 → 原答案

用法：python local_test/deep_water_solver.py [--input idx列表] [--max N]
"""
import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.deps import Deps
from llm_client import InternChatClient
from utils.time_budget import TimeBudget
from utils.llm_retry import chat_prefilled, thinking_mode_flag
from nodes.reasoning_agent_node import _parse_reasoning_output
from nodes.sc_consensus_node import same_answer
from utils.answer_verifier import verify_candidate
from utils.competition_methods import route_competition_method
from utils.rule_judge import is_closed_form_answer
import logging

_VIEW_HINTS = {
    "algebra": "\n求解视角：代数法——直接建立方程/不等式/递推关系，符号推导求解。",
    "construct": "\n求解视角：构造法——构造具体对象/反例/极端情形，先验证小规模再推广。",
    "enumerate": "\n求解视角：枚举法——小规模暴力枚举找规律，归纳通项或直接计数。",
    "algebra_low": "\n求解视角：代数法——直接建立方程/不等式/递推关系，符号推导求解。",
    "construct_high": "\n求解视角：构造法——构造具体对象/反例/极端情形，先验证小规模再推广。",
    "enumerate_low": "\n求解视角：枚举法——小规模暴力枚举找规律，归纳通项或直接计数。",
}
_TEMPS = {"algebra": 0.7, "construct": 1.0, "enumerate": 1.3,
          "algebra_low": 0.4, "construct_high": 1.5, "enumerate_low": 0.2}

_REASON_PROMPT = """你是一个数学专家。严格按以下格式解答（**最终答案必须写在最前面**）：

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

_CODE_PROMPT = """请为下面的数学题写一个完整可运行的 Python 程序求解（{code_hint}）。
最后一行必须 print("最终答案:", answer)，answer 是精确值（分数/根式/整数）。
只输出 ```python``` 代码块。

问题：
{problem}
"""

_CODE_HINTS = {
    "brute": "用暴力枚举/搜索（itertools/for 循环）直接计算，小规模验证后再放大。",
    "sympy": "用 sympy 符号计算（积分/方程/化简/矩阵），保持精确闭式。",
    "dp": "用动态规划/递推（自底向上填表），注意边界与初值。",
}


def _extract_code(resp):
    m = re.search(r"```python[ \t]*\n(.*?)\n```", resp or "", re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*\n?(.*?)\n?```", resp or "", re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


def _code_answer(stdout):
    m = re.search(r"最终答案[：:]\s*(.+)", stdout or "", re.MULTILINE)
    if m:
        return m.group(1).strip()
    lines = [l.strip() for l in (stdout or "").splitlines() if l.strip()]
    for l in reversed(lines):
        if re.fullmatch(r"[-+0-9/\\a-zA-Z\s\^\(\)\.\*\√π,_]+", l) and any(
                c.isdigit() for c in l):
            return l
    return ""


def solve_deep_water(idx, problem, official):
    from utils.deps import Deps
    from llm_client import InternChatClient
    from utils.time_budget import TimeBudget
    import logging
    client = InternChatClient()
    deps = Deps(client=client, time_budget=TimeBudget(1500),
                logger=logging.getLogger(f"deep_{idx}"))
    out = {"idx": idx, "official": str(official)[:80],
           "samples": [], "adopted": "", "reason": "", "correct": None}

    # 阶段1：采样（6 视角推理 + 3 代码）
    r_answers, c_answers = [], []
    framework = route_competition_method(problem)
    for view in ("algebra", "construct", "enumerate",
                 "algebra_low", "construct_high", "enumerate_low"):
        vh = _VIEW_HINTS[view]
        if view in ("construct", "construct_high") and framework:
            vh += framework
        prompt = _REASON_PROMPT.format(problem=problem[:6000], view_hint=vh)
        try:
            resp = chat_prefilled(
                client,
                messages=[{"role": "user", "content": prompt}],
                prefix="## 最终答案\n",
                temperature=_TEMPS[view],
                max_tokens=4096,
                logger=deps.logger,
                time_budget=deps.time_budget,
                label=f"dw_reason_{view}",
                thinking_mode=thinking_mode_flag(),
            )
        except Exception as exc:  # noqa: BLE001
            out["samples"].append({"view": view, "answer": "", "error": str(exc)[:80]})
            continue
        ans = (_parse_reasoning_output(resp or "") or {}).get("answer") or ""
        out["samples"].append({"view": view, "answer": ans[:300]})
        if ans:
            r_answers.append(ans)

    for algo in ("brute", "sympy", "dp"):
        prompt = _CODE_PROMPT.format(problem=problem[:6000],
                                     code_hint=_CODE_HINTS[algo])
        try:
            resp = chat_prefilled(
                client,
                messages=[{"role": "user", "content": prompt}],
                prefix="```python\n",
                temperature=0.4,
                max_tokens=4096,
                logger=deps.logger,
                time_budget=deps.time_budget,
                label=f"dw_code_{algo}",
                thinking_mode=thinking_mode_flag(),
            )
        except Exception as exc:  # noqa: BLE001
            out["samples"].append({"algo": algo, "answer": "", "error": str(exc)[:80]})
            continue
        code = _extract_code(resp or "")
        if not code:
            continue
        import subprocess
        try:
            r = subprocess.run([sys.executable, "-c", code],
                               capture_output=True, text=True, timeout=60,
                               cwd=str(Path(__file__).resolve().parent.parent))
            ans = _code_answer(r.stdout or "")
        except Exception:  # noqa: BLE001
            ans = ""
        out["samples"].append({"algo": algo, "answer": ans[:300]})
        if ans:
            c_answers.append(ans)

    # 阶段2：计票 → top-2
    scored = {}
    for a in r_answers + c_answers:
        key = str(a).strip()[:200]
        if not key:
            continue
        hit = None
        for k in scored:
            if same_answer(key, k):
                hit = k
                break
        if hit:
            scored[hit] += 1
        else:
            scored[key] = 1
    top = sorted(scored, key=scored.get, reverse=True)[:2]

    # 阶段3：验证 top-2（双独立代码验证）
    for cand in top:
        v = verify_candidate(client, deps, problem, cand)
        out["verify"] = out.get("verify", []) + [{"candidate": cand[:120],
                                                  "verdicts": v.get("verdicts", [])}]
        if v.get("verified"):
            out["adopted"] = cand
            out["reason"] = ("程序化双验证通过" if v.get("strong")
                             else "程序化弱验证通过")
            break
    # 阶段4：投票兜底（2/3 一致）
    if not out["adopted"] and top:
        if len(top) == 1 or scored.get(top[0], 0) >= max(2, len(r_answers) // 3):
            out["adopted"] = top[0]
            out["reason"] = "采样投票一致"

    if out["adopted"]:
        out["correct"] = same_answer(out["adopted"], official) is True
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=0)
    args = ap.parse_args()

    main_map = {}
    with open("local_test/data/omni_math/test.jsonl", encoding="utf-8") as f:
        for i, line in enumerate(f):
            d = json.loads(line)
            main_map[d["problem"].strip()] = d["answer"]

    # 深水区 = round4 retest 仍错 47 道
    rows = {}
    for line in open("local_test/data/schedule/set1_round_004.jsonl",
                     encoding="utf-8"):
        line = line.strip()
        if line:
            d = json.loads(line)
            rows[d["idx"]] = d
    g = json.load(open("local_test/results/set1_round_004_retest_v45_graded.json",
                       encoding="utf-8"))
    probs = g.get("problems", g) if isinstance(g, dict) else g
    qs = []
    for p in probs:
        if p.get("llm_verdict") != "correct":
            q = rows.get(p.get("idx"))
            if q:
                qs.append({"idx": p.get("idx"), "problem": q["problem"],
                           "official": main_map.get(q["problem"].strip(), "?")})
    qs.sort(key=lambda x: x["idx"])
    if args.max:
        qs = qs[:args.max]
    print(f"深水区攻坚: {len(qs)} 题（9 采样 + top-2 双验证）")

    results = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(solve_deep_water, q["idx"], q["problem"],
                            q["official"]): q["idx"] for q in qs}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            mark = "✓" if r.get("correct") else "✗"
            print(f"[{i}/{len(qs)}] #{r['idx']} {mark} [{r.get('reason','')}] "
                  f"adopted={str(r.get('adopted',''))[:45]}")

    saved = [r["idx"] for r in results if r.get("correct")]
    print(f"\n=== 深水区攻坚结果 ===")
    print(f"救回: {len(saved)}/{len(results)} | idx: {saved}")
    print(f"耗时: {(time.monotonic()-t0)/60:.1f} min")
    Path("local_test/results/deep_water_1.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
