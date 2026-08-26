"""V4.6 SC 共识节点：3 视角推理采样 + 3 份代码求解 + 投票锁定。

实验依据（local_test/sc_experiment.py，47 题最难边界题救回 5/47）：
- 3 视角推理（代数/构造/枚举 × 温度 0.7/1.0/1.3）打破同源（#55/#87 原
  三路候选同错，采样出新正确路径）
- 3 份代码（brute/sympy/dp）算法互补（#14/#63 原 python 全失败）
- 双通道一致（#34：推理 ln2 == 代码 log(2)）是最强锁定信号

触发：validation 未 match + hard/规则可判定 + remaining_hard > 600s。
预算：每通道最多 3 份 × 4096 tokens，早停（2/3 一致即停）。
锁定：任通道 ≥2 一致 → answer_locked=True；全不一致 → 走原链路不更差。
"""
from __future__ import annotations

import logging
import re

from utils.llm_retry import chat_prefilled, thinking_mode_flag
from utils.token_budget import estimate_tokens
from config import CONFIG

logger = logging.getLogger("sc_consensus")

#: 采样视角（V4.8 规模化：3 视角 × 2 温度轮 = 6 份；实验实证单视角
#: 命中率仅 ~12%，n 份采样至少一次对 = 1-(1-p)^n，6 份 ≈ 54%）。
#: V5.1 动态预算适配：保留 6 视角 + 3 代码全火力（准确率不降），
#: 由 SC 阶段预算封顶（sc_end = remaining-400）自动控制——预算充足跑满，
#: 预算紧张自动少跑（官方 6h 完成率保底，快 API 准确率拉满）。
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

#: 主图 SC 全火力（动态预算适配：完整视角 + 完整代码，由 sc_end 控制）
_SC_VIEWS = ("algebra", "construct", "enumerate",
             "algebra_low", "construct_high", "enumerate_low")
_SC_CODE_ALGOS = ("brute", "sympy", "dp")

_CODE_HINTS = {
    "brute": "用暴力枚举/搜索（itertools/for 循环）直接计算，小规模验证后再放大。",
    "sympy": "用 sympy 符号计算（积分/方程/化简/矩阵），保持精确闭式。",
    "dp": "用动态规划/递推（自底向上填表），注意边界与初值。",
}

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


def _text_equiv(a: str, b: str) -> bool:
    """文本等价：去空白/标点后一致（文本答案投票通道，实验实证 #1/#7 类）。"""
    if not a or not b:
        return False
    ka = re.sub(r"[\s。．.,，;；:：!！?？\"'“”‘’()（）\[\]【】\-_]+", "", str(a))
    kb = re.sub(r"[\s。．.,，;；:：!！?？\"'“”‘’()（）\[\]【】\-_]+", "", str(b))
    return bool(ka) and ka.lower() == kb.lower() and len(ka) >= 3


def same_answer(a: str, b: str) -> bool:
    """闭式等价或文本等价任一通过即视为同一答案。"""
    try:
        from utils.rule_judge import (
            is_closed_form_answer, answers_numerically_equivalent)
        if is_closed_form_answer(a) and is_closed_form_answer(b):
            eq = answers_numerically_equivalent(a, b)
            if eq is not None:
                return eq
    except Exception:  # noqa: BLE001 - 判定失败回退文本等价
        pass
    return _text_equiv(a, b)


def _wants_sc(state: dict, config) -> bool:
    """触发条件：未 match + hard/规则可判定 + 预算充足 + 未跑过。"""
    if not CONFIG.get("enable_sc_consensus", True):
        return False
    if state.get("sc_consensus_done"):
        return False  # 每题只跑一次（修复循环重入时跳过，防重复消费）
    # MockClient（集成测试环境）：SC 是真实采样节点，mock 响应无意义且会
    # 破坏固定响应顺序——测试环境直接跳过（SC 逻辑由 test_sc_consensus
    # 与真机实验覆盖）。
    try:
        from utils.deps import MockClient, get_deps
        if isinstance(get_deps(config).client, MockClient):
            return False
    except Exception:  # noqa: BLE001 - 检测失败按真实环境处理
        pass
    if state.get("validation_status") in ("match", None):
        return False
    if state.get("question_mode") == "proof":
        return False  # 证明题走既有 proof 链路（build_proof_body）
    # V5.1 调用量压缩（官方 6h 合规：round6 实测 2627 次/轮，官方并发 3
    # 慢 API 下超时风险）：SC 只对"规则可判定或计算特征词"题触发——
    # 文本/纯概念题 SC 采样收益低（无闭式可验证），省调用给计算题。
    diff = str(state.get("difficulty") or "").lower()
    _is_compute = False
    try:
        from utils.rule_judge import state_is_rule_judgeable
        _is_compute = state_is_rule_judgeable(state)
    except Exception:  # noqa: BLE001 - 判定器故障按特征词回退
        _is_compute = False
    if not _is_compute:
        import re as _re
        problem = str(state.get("problem") or "")
        _is_compute = bool(_re.search(
            r"how many|number of|probability|compute|evaluate|find the (?:value|"
            r"minimum|maximum)|sum of|枚举|多少个|概率|计算|求[出]?.{0,4}(值|结果)",
            problem, _re.IGNORECASE))
    if not _is_compute:
        return False  # 文本/概念题不触发 SC（省调用）
    # V5.1-修正：easy 题**恢复** SC 触发——round6 实证 easy 仅 50%
    # （#0/#29 同源同错 match 锁错、#13 假性放弃），SC 多视角采样对
    # 简单题命中率极高（6 视角几乎必中）且成本低（主链路快、预算充足）。
    # 之前"easy 不触发"是错误压缩，已回滚。
    try:
        from utils.deps import get_deps
        clock = get_deps(config).time_budget
        if clock is not None and clock.remaining_hard() < 600:
            return False
    except Exception:  # noqa: BLE001 - 预算取不到时按可负担处理
        pass
    return True


def _extract_code(response: str) -> str:
    m = re.search(r"```python[ \t]*\n(.*?)\n```", response or "", re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*\n?(.*?)\n?```", response or "", re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


def _answer_from_stdout(stdout: str) -> str:
    m = re.search(r"最终答案[：:]\s*(.+)", stdout or "", re.MULTILINE)
    if m:
        return m.group(1).strip()
    lines = [l.strip() for l in (stdout or "").splitlines() if l.strip()]
    for l in reversed(lines):
        if re.fullmatch(r"[-+0-9/\\a-zA-Z\s\^\(\)\.\*\√π,_]+", l) \
                and any(c.isdigit() for c in l):
            return l
    return ""


def sc_consensus_node(state, config):
    """SC 共识主入口：返回带 sc_consensus 记录的 state 扩展。"""
    deps = __import__("utils.deps", fromlist=["get_deps"]).get_deps(config)
    problem = state.get("problem", "")
    out = {"sc_consensus": None}
    if not _wants_sc(state, config):
        # 保持已有 done 标志（重入时不覆盖 True，避免修复循环重复跑 SC）
        out["sc_consensus_done"] = bool(state.get("sc_consensus_done"))
        return out
    client = deps.client
    budget = deps.token_budget
    clock = deps.time_budget
    mcp_client = deps.mcp_client
    # V5.1 官方预算合规（1200s 单题硬限）：SC 阶段预算封顶——进入时的
    # remaining_hard 只允许消耗到 <400s 为止，保证 coordinator 至少有
    # 400s 收尾（官方 6h/并发 3 下每题 ~580s 墙钟余量）。
    sc_end = None
    if clock is not None:
        try:
            sc_end = max(400, clock.remaining_hard() - 400)
        except Exception:  # noqa: BLE001
            sc_end = None

    r_answers: list[str] = []
    r_samples = []
    # V4.9 竞赛方法论增强：构造/枚举视角叠加按题面路由的竞赛方法框架
    # （不变量/SG/生成函数/极值原理等——提升单次命中率，直接增强
    # 采样-验证-确认攻坚管线）。
    from utils.competition_methods import route_competition_method
    method_framework = route_competition_method(problem)
    for view in _SC_VIEWS:
        if clock is not None and clock.remaining_hard() < 300:
            break
        if sc_end is not None and clock.remaining_hard() < sc_end:
            break  # V5.1 SC 阶段预算封顶（保 coordinator 收尾）
        view_hint = _VIEW_HINTS[view]
        if view in ("construct", "construct_high") and method_framework:
            view_hint += method_framework
        prompt = _REASON_PROMPT.format(problem=problem[:6000],
                                       view_hint=view_hint)
        try:
            resp = chat_prefilled(
                client,
                messages=[{"role": "user", "content": prompt}],
                prefix="## 最终答案\n",
                temperature=_TEMPS[view],
                max_tokens=4096,
                logger=deps.logger,
                time_budget=clock,
                label=f"sc_reason_{view}",
                thinking_mode=thinking_mode_flag(),
            )
        except Exception as exc:  # noqa: BLE001
            r_samples.append({"view": view, "answer": "", "error": str(exc)[:120]})
            continue
        if budget:
            budget.consume(estimate_tokens(prompt), estimate_tokens(resp))
        try:
            from nodes.reasoning_agent_node import _parse_reasoning_output
            ans = (_parse_reasoning_output(resp or "") or {}).get("answer") or ""
        except Exception:  # noqa: BLE001
            ans = ""
        r_samples.append({"view": view, "answer": ans[:400]})
        if not ans:
            continue
        r_answers.append(ans)
        if len(r_answers) >= 2 and same_answer(r_answers[0], r_answers[1]):
            break  # 早停

    # 推理通道 2/3 一致 → 锁定
    r_consensus = None
    if len(r_answers) >= 2:
        for a in r_answers:
            if sum(1 for x in r_answers if same_answer(x, a)) >= 2:
                r_consensus = a
                break

    c_answers: list[str] = []
    c_samples = []
    if r_consensus is None:  # 推理未一致才花代码预算
        for algo in _SC_CODE_ALGOS:
            if clock is not None and clock.remaining_hard() < 200:
                break
            if sc_end is not None and clock.remaining_hard() < sc_end:
                break  # V5.1 SC 阶段预算封顶
            if mcp_client is None:
                break
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
                    time_budget=clock,
                    label=f"sc_code_{algo}",
                    thinking_mode=thinking_mode_flag(),
                )
            except Exception as exc:  # noqa: BLE001
                c_samples.append({"algo": algo, "answer": "", "error": str(exc)[:120]})
                continue
            if budget:
                budget.consume(estimate_tokens(prompt), estimate_tokens(resp))
            code = _extract_code(resp or "")
            if not code:
                c_samples.append({"algo": algo, "answer": "", "error": "no code"})
                continue
            output = mcp_client.execute(code, timeout=60)
            ans = _answer_from_stdout(str(output.get("stdout") or ""))
            c_samples.append({"algo": algo, "answer": ans[:400],
                              "success": bool(output.get("success"))})
            if not ans:
                continue
            c_answers.append(ans)
            if len(c_answers) >= 2 and same_answer(c_answers[0], c_answers[1]):
                break  # 早停

    c_consensus = None
    if len(c_answers) >= 2:
        for a in c_answers:
            if sum(1 for x in c_answers if same_answer(x, a)) >= 2:
                c_consensus = a
                break

    # 双通道一致 → 最高置信；单通道一致 → 锁定该通道
    adopted, reason = None, ""
    if r_consensus and c_consensus and same_answer(r_consensus, c_consensus):
        adopted, reason = r_consensus, "双通道一致（推理+代码）"
    elif r_consensus:
        adopted, reason = r_consensus, "推理采样 2/3 一致"
    elif c_consensus:
        adopted, reason = c_consensus, "代码求解 2/3 一致"
    if adopted is None:
        # V4.7 SC 散文接管（round5 实证 #10：主链路 validated 是散文
        # "验证无更小值"，SC 采样有闭式 -1/8 但 3 份未达成 2/3 一致）：
        # 主链路候选非闭式（散文/放弃）+ SC 有**唯一**闭式采样 → 锁该闭式
        # （闭式 > 散文原则，同 V4.4 diverse 接管；多个不同闭式不锁）。
        try:
            from utils.rule_judge import is_closed_form_answer
            main_ans = str(state.get("validated_answer") or "").strip()
            if main_ans and not is_closed_form_answer(main_ans):
                all_samples = (r_answers[:] + c_answers[:])
                closed = [a for a in all_samples if is_closed_form_answer(a)]
                if closed:
                    unique = []
                    for a in closed:
                        if not any(same_answer(a, u) for u in unique):
                            unique.append(a)
                    if len(unique) == 1:
                        adopted, reason = unique[0], "SC 散文接管（主链路散文→闭式采样）"
        except Exception:  # noqa: BLE001 - 接管失败保持原链路
            pass
    if adopted is None:
        # V4.9 SVC 阶段 3：投票未决时对 top-2 候选做**程序化验证**——
        # 验证器（代入/枚举对照/反例搜索）是确定性证据，强于投票。
        # 只验证 top-2（预算：2 候选 × 2 验证代码 ≈ 4 调用/题）。
        try:
            from utils.answer_verifier import verify_candidate
            scored: dict[str, int] = {}
            for a in r_answers + c_answers:
                key = str(a).strip()[:200]
                if not key:
                    continue
                if any(same_answer(key, k) for k in scored):
                    for k in scored:
                        if same_answer(key, k):
                            scored[k] += 1
                            break
                else:
                    scored[key] = 1
            top = sorted(scored, key=scored.get, reverse=True)[:2]
            for cand in top:
                if clock is not None and clock.remaining_hard() < 200:
                    break
                v = verify_candidate(client, deps, problem, cand)
                if v.get("verified"):
                    adopted, reason = cand, (
                        "程序化双验证通过" if v.get("strong")
                        else "程序化弱验证通过")
                    break
        except Exception as exc:  # noqa: BLE001 - 验证失败走原链路
            deps.logger.warning("SC verifier failed: %s", exc)

    out["sc_consensus"] = {
        "adopted": (adopted or "")[:400],
        "reason": reason,
        "r_samples": r_samples,
        "c_samples": c_samples,
        "r_answers": r_answers[:3],
        "c_answers": c_answers[:3],
        "r_consensus": r_consensus,
        "c_consensus": c_consensus,
    }
    if adopted:
        out["validated_answer"] = adopted
        out["answer_locked"] = True
        out["sc_adopted"] = True
        deps.logger.info("SC consensus locked: %s (%s)", adopted[:60], reason)
    return out
