# -*- coding: utf-8 -*-
"""深水区迭代攻坚求解器（V5.0：IMO25 金牌管线——验证-精炼循环）。

对每道深水区题做最多 4 轮迭代：
  轮 1: 9 采样（6 推理竞赛方法注入 + 3 代码）→ 计票 top-2 → 双验证
  通过 → 锁定；FAIL → 提取反例反馈
  轮 2-4: 5 采样（提示含上一轮验证反馈约束）→ 验证 top-2 → 循环
  4 轮后未锁定 → 投票最高票 + 标记"unconfirmed"

合规：官方 API + sympy + MCP 沙箱，无外部模型/训练/Lean。

用法：python local_test/deep_water_solver.py [--max N] [--rounds 4]
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

from utils.deps import Deps
from llm_client import InternChatClient
from utils.time_budget import TimeBudget
from utils.llm_retry import chat_prefilled, thinking_mode_flag
from nodes.reasoning_agent_node import _parse_reasoning_output
from nodes.sc_consensus_node import same_answer
from utils.answer_verifier import verify_candidate
from utils.verifier_feedback import extract_feedback
from utils.competition_methods import route_competition_method
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
{feedback_block}
问题：
{problem}
"""

_CODE_PROMPT = """请为下面的数学题写一个完整可运行的 Python 程序求解（{code_hint}）。
最后一行必须 print("最终答案:", answer)，answer 是精确值（分数/根式/整数）。
只输出 ```python``` 代码块。
{feedback_block}
问题：
{problem}
"""

_CODE_HINTS = {
    "brute": "用暴力枚举/搜索（itertools/for 循环）直接计算，小规模验证后再放大。",
    "sympy": "用 sympy 符号计算（积分/方程/化简/矩阵），保持精确闭式。",
    "dp": "用动态规划/递推（自底向上填表），注意边界与初值。",
}

# V5.3 交错推理（InternLM-Math agent 路线：语言→代码→结果→语言逐步交错，
# 实验实证救回 SC/代码/管线都没救过的 #46 #62——逐步计算补上"中间量
# 修正"路径）。增强：竞赛方法路由注入 + 执行失败自动重写。
_IL_SYSTEM = """你是数学专家。解题时采用**交错推理**：
- 每步先写推理文本（简短）；
- 如果这一步需要计算/验证（代入、枚举、符号化简、数值检验），必须输出
  ```python 代码块``` 来计算——代码执行结果会反馈给你，继续下一步推理；
- 不要一次写出所有代码；逐步计算，每步依赖上一步的执行结果；
- 当你确定最终答案时，最后单独一行输出：最终答案：<答案>。

{method_framework}"""

_VIEWS = ("algebra", "construct", "enumerate",
          "algebra_low", "construct_high", "enumerate_low")


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


def _interleaved_solve(client, deps, problem, framework, feedback,
                       max_steps=4):
    """V5.3 交错推理：语言→代码→结果→语言逐步交错（最多 max_steps 步）。
    返回最终答案字符串（无则 ""）。"""
    system = _IL_SYSTEM.format(method_framework=framework)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": problem[:6000] + feedback}]
    final = ""
    for _step in range(max_steps):
        if deps.time_budget and deps.time_budget.remaining_hard() < 200:
            break
        try:
            resp = chat_prefilled(
                client,
                messages=messages,
                prefix="",
                temperature=0.4,
                max_tokens=4096,
                logger=deps.logger,
                time_budget=deps.time_budget,
                label="il_step",
                thinking_mode=thinking_mode_flag(),
            )
        except Exception:  # noqa: BLE001
            break
        text = (resp or "").strip()
        m = re.search(r"最终答案[：:]\s*(.+)$", text, re.MULTILINE)
        if m:
            final = m.group(1).strip()
            break
        code = _extract_code(text)
        messages.append({"role": "assistant", "content": text})
        if not code:
            messages.append({"role": "user",
                             "content": "请继续推理。如果下一步需要计算，输出 ```python 代码块```；"
                                        "确定答案时输出：最终答案：<答案>"})
            continue
        try:
            r = subprocess.run([sys.executable, "-c", code],
                               capture_output=True, text=True, timeout=90,
                               cwd=str(Path(__file__).resolve().parent.parent))
            stdout, stderr = (r.stdout or "")[:600], (r.stderr or "")[:200]
        except subprocess.TimeoutExpired:
            stdout, stderr = "", "TIMEOUT"
        except Exception as exc:  # noqa: BLE001
            stdout, stderr = "", str(exc)[:200]
        env = "代码执行结果：\n" + stdout if stdout else ""
        if stderr:
            env += "\n执行错误：\n" + stderr + "\n请修正代码后重新计算这一步。"
        messages.append({"role": "user", "content": env or "（无输出）"})
    return final


def _sample_round(client, deps, problem, framework, feedback, n_views,
                  n_codes):
    """一轮采样：n_views 视角推理 + 1 次交错推理 + n_codes 份代码。返回答案列表。"""
    answers = []
    for view in _VIEWS[:n_views]:
        vh = _VIEW_HINTS[view]
        if view in ("construct", "construct_high") and framework:
            vh += framework
        prompt = _REASON_PROMPT.format(problem=problem[:6000], view_hint=vh,
                                       feedback_block=feedback)
        try:
            resp = chat_prefilled(
                client,
                messages=[{"role": "user", "content": prompt}],
                prefix="## 最终答案\n",
                temperature=_TEMPS[view],
                max_tokens=4096,
                logger=deps.logger,
                time_budget=deps.time_budget,
                label="dw_reason",
                thinking_mode=thinking_mode_flag(),
            )
        except Exception:  # noqa: BLE001
            continue
        ans = (_parse_reasoning_output(resp or "") or {}).get("answer") or ""
        if ans:
            answers.append(ans)
    for algo in ("brute", "sympy", "dp")[:n_codes]:
        prompt = _CODE_PROMPT.format(problem=problem[:6000],
                                     code_hint=_CODE_HINTS[algo],
                                     feedback_block=feedback)
        try:
            resp = chat_prefilled(
                client,
                messages=[{"role": "user", "content": prompt}],
                prefix="```python\n",
                temperature=0.4,
                max_tokens=4096,
                logger=deps.logger,
                time_budget=deps.time_budget,
                label="dw_code",
                thinking_mode=thinking_mode_flag(),
            )
        except Exception:  # noqa: BLE001
            continue
        code = _extract_code(resp or "")
        if not code:
            continue
        try:
            r = subprocess.run([sys.executable, "-c", code],
                               capture_output=True, text=True, timeout=60,
                               cwd=str(Path(__file__).resolve().parent.parent))
            ans = _code_answer(r.stdout or "")
        except Exception:  # noqa: BLE001
            ans = ""
        if ans:
            answers.append(ans)
    # V5.3 交错推理采样（每轮 1 次，逐步代码计算路径——补视角发散之外
    # 的"中间量修正"路径，实验实证救回 #46/#62 类）。
    il_ans = _interleaved_solve(client, deps, problem, framework, feedback)
    if il_ans:
        answers.append(il_ans)
    return answers


def _top_candidates(answers, k=2):
    """计票去重 → top-k。"""
    scored = {}
    for a in answers:
        key = str(a).strip()[:200]
        if not key:
            continue
        hit = None
        for kk in scored:
            if same_answer(key, kk):
                hit = kk
                break
        if hit:
            scored[hit] += 1
        else:
            scored[key] = 1
    return sorted(scored, key=scored.get, reverse=True)[:k], scored


def solve_deep_water(idx, problem, official, max_rounds=4):
    client = InternChatClient()
    from utils.python_mcp_client import PythonMCPClient
    mcp = PythonMCPClient()
    # V5.0-fix：迭代攻坚需 33+ 次调用/题（采样+双验证），默认 cap 26 会
    # 耗尽导致验证器无法执行（小样本实测 0/5 根因）；1800s 覆盖 3 轮慢 API。
    deps = Deps(client=client, mcp_client=mcp,
                time_budget=TimeBudget(1800, max_llm_calls=90),
                logger=logging.getLogger(f"deep_{idx}"))
    out = {"idx": idx, "official": str(official)[:80],
           "rounds": [], "adopted": "", "reason": "", "correct": None}
    framework = route_competition_method(problem)
    feedback = ""
    history = []  # 所有已试候选（防重复采样）

    for rnd in range(1, max_rounds + 1):
        # V5.0-fix：轮次采样递减（9/5/4）+ 时间守卫——验证器必须能跑
        # （小样本实测：3 轮各 6-9 份采样耗尽 1800s，验证器从未执行）。
        # V5.1（RPM 30 限流适配）：轮 1 = 4 视角 + 1 代码（5 采样），
        # 后续轮 3 视角（3 采样）——单轮命中率 ~30%，限流下效率最优。
        n_views = 4 if rnd == 1 else 3
        n_codes = 1 if rnd == 1 else 0
        # 采样前留足验证时间（验证 top-2 需 ~4 次调用 × 80s ≈ 320s）
        if deps.time_budget and deps.time_budget.remaining_hard() < 400:
            break
        answers = _sample_round(client, deps, problem, framework, feedback,
                                n_views, n_codes)
        new_answers = [a for a in answers
                       if not any(same_answer(a, h) for h in history)]
        history.extend(new_answers)
        top, scored = _top_candidates(new_answers or answers)
        round_info = {"round": rnd, "n_answers": len(new_answers),
                      "top": [str(t)[:120] for t in top]}
        # 验证 top-2（验证前守卫：至少 250s 剩余）
        locked = None
        locked_reason = ""
        for cand in top:
            if deps.time_budget and deps.time_budget.remaining_hard() < 250:
                break
            v = verify_candidate(client, deps, problem, cand)
            round_info["verifies"] = round_info.get("verifies", []) + [
                {"candidate": str(cand)[:120],
                 "verdicts": v.get("verdicts", [])}]
            if v.get("verified"):
                locked, locked_reason = cand, (
                    "程序化双验证通过" if v.get("strong") else "程序化弱验证通过")
                break
        out["rounds"].append(round_info)
        if locked:
            out["adopted"], out["reason"] = locked, locked_reason
            break
        # 提取反例反馈喂下一轮
        fb_parts = []
        for vv in round_info.get("verifies", []):
            if "FAIL" in vv.get("verdicts", []):
                # 重新跑一次验证拿 stdout 不划算——用 verdicts 摘要
                fb_parts.append(f"候选 {vv['candidate'][:60]} 验证 FAIL")
        if fb_parts:
            feedback = "上一轮候选验证失败：" + "；".join(fb_parts) + \
                "。重解时必须给出**能被程序验证通过**的答案（代入检查/枚举对照），避免重复错误。"
        else:
            feedback = ""
    # 4 轮后未锁：投票最高票
    if not out["adopted"] and history:
        top, scored = _top_candidates(history, 1)
        if top:
            out["adopted"] = top[0]
            out["reason"] = "多轮采样投票最高票（未经验证，unconfirmed）"
    if out["adopted"]:
        out["correct"] = same_answer(out["adopted"], official) is True
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--rounds", type=int, default=4)
    args = ap.parse_args()

    main_map = {}
    with open("local_test/data/omni_math/test.jsonl", encoding="utf-8") as f:
        for i, line in enumerate(f):
            d = json.loads(line)
            main_map[d["problem"].strip()] = d["answer"]

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
    print(f"深水区迭代攻坚: {len(qs)} 题 × {args.rounds} 轮（采样+双验证+反例反馈）")

    results = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(solve_deep_water, q["idx"], q["problem"],
                            q["official"], args.rounds): q["idx"] for q in qs}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            mark = "✓" if r.get("correct") else "✗"
            rounds = len(r.get("rounds", []))
            print(f"[{i}/{len(qs)}] #{r['idx']} {mark} 轮数={rounds} "
                  f"[{r.get('reason','')}] adopted={str(r.get('adopted',''))[:40]}")

    saved = [r["idx"] for r in results if r.get("correct")]
    confirmed = [r["idx"] for r in results
                 if r.get("correct") and "验证" in r.get("reason", "")]
    print(f"\n=== 深水区迭代攻坚结果 ===")
    print(f"救回: {len(saved)}/{len(results)} | idx: {saved}")
    print(f"其中程序化验证确认: {len(confirmed)}")
    print(f"耗时: {(time.monotonic()-t0)/60:.1f} min")
    Path("local_test/results/deep_water_iter.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
