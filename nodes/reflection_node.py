"""深度反思节点（VeritasMath Reflection，V2.6）：Critic 审计前对答案做"正确性"批判性复核。

与 Critic 的职责严格区分：
- Critic 审"答案是否完整覆盖题面要求"（缺项/契约）+ 抽核 1-3 个中间计算；
- Reflection 审"答案本身对不对"（代回验证/差一/符号/边界/反例）。

这是 V2.5 之后追 80+ 的核心改动：此前 reasoning+python 两路"match 一致"就
直达 coordinator 出厂——即使两路**一致地错**（单样本系统性错误），也没有任何
环节回答"答案到底对不对"。官方实测 retry_count=0 正是这个缺口的铁证：首轮
一致就出答案，从不反思。

触发纪律（避免 V2.1 超时覆辙）：
- 只对 hard 难度 或 低置信（confidence < 阈值）的非客观题触发；
- 每道题最多反思 1 次（reflection_round 计数）；
- 预算不足、解析失败、超时一律放行原答案（不阻塞主链路）；
- 判 WRONG 时生成定向修复提示，复用 reconciliation 既有重算回路（合规：
  定向纠错，非同 prompt 重复采样）。
"""

from __future__ import annotations

from config import CONFIG
from utils.cot_stripper import is_placeholder_answer
from utils.critic_audit import _parse_json_object
from utils.deps import get_deps
from utils.llm_retry import chat_prefilled
from utils.problem_profile import is_objective_mode
from utils.prompt_templates import (
    REFLECTION_PREFILL,
    REFLECTION_PROMPT,
    REFLECTION_SYSTEM_PROMPT,
)
from utils.reconciliation_policy import reconciliation_retry_available
from utils.token_budget import estimate_tokens

#: 反思是轻量判断，prefill 后输出 ~200-400 token；给 1024 防截断。
_REFLECTION_MAX_TOKENS = 1024
_REFLECTION_CALL_ESTIMATE_S = 25
#: 每道题最多反思 1 次。
_MAX_REFLECTION_ROUNDS = 1


def _bounded(value, limit: int) -> str:
    text = value if isinstance(value, str) else str(value or "")
    return text if len(text) <= limit else text[:limit].rstrip() + "\n...[truncated]"


def _process_summary(state: dict) -> str:
    """供反思参考的过程摘要：推理分析 + 步骤 + Python 证据，定长截断。"""
    rr = state.get("reasoning_result") or {}
    po = state.get("python_output") or {}
    parts = []
    analysis = str(rr.get("analysis") or "").strip()
    if analysis:
        parts.append("推理分析：" + _bounded(analysis, 1500))
    steps = [f"步骤{s.get('step_num', '?')}: {s.get('description', '')}"
             for s in (rr.get("steps") or [])][:12]
    if steps:
        parts.append("推理步骤：\n" + _bounded("\n".join(steps), 2500))
    evidence = str(po.get("evidence_summary") or state.get("python_evidence_summary") or "").strip()
    if evidence:
        parts.append("程序验证证据：" + _bounded(evidence, 800))
    return "\n\n".join(parts) or "（无过程记录）"


def should_reflect(state: dict) -> bool:
    """该题是否应进入反思环。

    触发条件（全部满足）：
    - 开关开启；非客观题；首轮（未反思过）。

    不依赖分类器的 difficulty/confidence 信号——实测证明分类器不可靠：
    把 level 4 科研级题（球面平均曲率、Z15 乘法群阶）判成 easy + 0.98 高置信，
    但模型在这些"高置信"题上照样答错。反思环的价值恰恰是抓"高置信地错"
    （两路一致地错，retry_count=0 的根因），因此对非客观题一律反思一次。
    成本：单次反思 ~25s（prefill），非客观题约 80 题 ≈ +33min 墙钟，
    判 wrong 后重算另计，均落在 V2.4 实测 2.5h/6h 的余量内。
    """
    if not CONFIG.get("enable_reflection", True):
        return False
    if is_objective_mode(state.get("question_mode", "")):
        return False
    if state.get("reflection_round", 0) >= _MAX_REFLECTION_ROUNDS:
        return False
    return True


def parse_reflection_verdict(raw: str) -> dict | None:
    """解析反思回复为标准判定 dict；解析失败返回 None。

    标准形态：{"verdict": "correct|wrong", "error_step": str,
    "fix_hint": str, "reason": str}
    """
    parsed = _parse_json_object(raw)
    if parsed is None:
        return None
    verdict = str(parsed.get("verdict") or "").strip().lower()
    if verdict not in ("correct", "wrong"):
        return None
    return {
        "verdict": verdict,
        "error_step": _bounded(parsed.get("error_step") or "", 300),
        "fix_hint": _bounded(parsed.get("fix_hint") or "", 300),
        "reason": _bounded(parsed.get("reason") or "", 300),
    }


def _llm_reflect(state: dict, deps, answer: str) -> dict | None:
    """prefill 反思调用；失败/超时/解析失败返回 None（放行，不阻塞）。"""
    clock = deps.time_budget
    margin = CONFIG.get("reflection_reserve_margin_s", 60)
    if clock and clock.remaining_hard() - margin < _REFLECTION_CALL_ESTIMATE_S:
        return None
    prompt = REFLECTION_PROMPT.format(
        problem=_bounded(state.get("problem", ""), 8000),
        process_summary=_process_summary(state),
        answer=_bounded(answer, 6000),
    )
    try:
        raw = chat_prefilled(
            deps.client,
            messages=[
                {"role": "system", "content": REFLECTION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            prefix=REFLECTION_PREFILL,
            temperature=0.1,
            max_tokens=_REFLECTION_MAX_TOKENS,
            logger=deps.logger,
            time_budget=clock,
            expected_call_seconds=_REFLECTION_CALL_ESTIMATE_S,
            label="reflection_prefill",
            reserve_margin_s=margin,
        )
    except Exception as exc:  # noqa: BLE001 - 反思失败不阻塞出答案
        deps.logger.warning("Reflection unavailable: %s", exc)
        return None
    if deps.token_budget:
        deps.token_budget.consume(estimate_tokens(prompt), estimate_tokens(raw))
    return parse_reflection_verdict(raw)


_ANCHOR_NUM_RE = __import__("re").compile(
    r"(?:(?:正确)?(?:结果|答案|值)\s*(?:是|为)|应该是|应是|应为)\s*[^，。；、]{0,60}"
)


def _de_anchor(text: str) -> str:
    """剥离 fix_hint 里可能的数值锚定。

    反思 LLM 与主模型同源，它复核时也可能算错（idx=28 实证：反思说"应为 198"，
    实际正确是 748）。若把"正确结果应为 X"原样传给重算，可能把对的答案带偏。
    这里剥离"应为 X / 结果是 X"类锚定，只保留错误性质，让重算独立推理。
    """
    cleaned = _ANCHOR_NUM_RE.sub("请独立重算验证", text)
    return cleaned.strip("，。；、 ")


def _build_fix_hint(verdict: dict, answer: str) -> str:
    """把反思发现的错误转成给 solving 子图的定向修复提示。"""
    parts = ["上一次解答经批判性复核发现实质错误，需要修正。"]
    if verdict.get("error_step"):
        parts.append(f"错误位置：{verdict['error_step']}。")
    fix_hint = _de_anchor(str(verdict.get("fix_hint") or ""))
    if fix_hint:
        parts.append(f"修正方向：{fix_hint}。")
    current = str(answer or "").strip()
    if current:
        parts.append(f"当前错误答案（供对照，不要沿用）：{current[:600]}")
    parts.append("请重新独立推理，重点修正上述错误；不要锚定任何预设答案，"
                 "保持正确部分不变，严格按原格式输出"
                 "（含 '## 问题分析'、'## 详细解题步骤'、'## 最终答案' 章节）。")
    return "\n".join(parts)


def reflection_node(state: dict, config: dict) -> dict:
    """反思节点：Critic 审计之后、coordinator 之前对答案做正确性批判性复核。

    返回：
    - 不该反思 / 无答案 / 反思确认正确 / 反思失败 → next_node="coordinator"
    - 反思发现实质错误且可重算 → 定向修复提示 + next_node="reconciliation"
    """
    deps = get_deps(config)
    trace = list(state.get("reflection_trace") or [])
    rounds = state.get("reflection_round", 0)

    if not CONFIG.get("enable_reflection", True):
        return {"reflection_status": "skipped", "reflection_trace": trace,
                "reflection_round": rounds, "next_node": "coordinator"}
    if not should_reflect(state):
        trace.append({"round": rounds, "status": "skipped", "reason": "not_triggered"})
        return {"reflection_status": "skipped", "reflection_trace": trace,
                "reflection_round": rounds, "next_node": "coordinator"}

    answer = str(state.get("validated_answer") or "").strip()
    if not answer:
        rr = state.get("reasoning_result") or {}
        answer = str(rr.get("answer") or "").strip()
    if not answer or is_placeholder_answer(answer):
        trace.append({"round": rounds, "status": "skipped", "reason": "no_answer"})
        return {"reflection_status": "skipped", "reflection_trace": trace,
                "reflection_round": rounds, "next_node": "coordinator"}

    verdict = _llm_reflect(state, deps, answer)
    if verdict is None:
        trace.append({"round": rounds, "status": "pass", "reason": "reflect_unavailable"})
        return {"reflection_status": "correct", "reflection_trace": trace,
                "reflection_round": rounds, "next_node": "coordinator"}

    trace.append({
        "round": rounds,
        "status": verdict["verdict"],
        "error_step": verdict.get("error_step", ""),
        "fix_hint": verdict.get("fix_hint", ""),
        "reason": verdict.get("reason", ""),
    })

    if verdict["verdict"] == "correct":
        return {"reflection_status": "correct", "reflection_trace": trace,
                "reflection_round": rounds, "next_node": "coordinator"}

    # 发现实质错误：可负担且未超修复轮次时回调解做定向重算；否则带着
    # 反思标记进 coordinator（不阻断，反思是锦上添花的救回通道）。
    can_repair = reconciliation_retry_available(state, config)
    if can_repair:
        hint = _build_fix_hint(verdict, answer)
        return {
            "reflection_status": "wrong",
            "reflection_trace": trace,
            "reflection_round": rounds + 1,
            "reasoning_retry_hint": hint,
            "python_retry_hint": hint + "\n请用代码独立验证修正后的结论。",
            "repair_hint_source": "reflection",
            "next_node": "reconciliation",
        }
    return {"reflection_status": "wrong", "reflection_trace": trace,
            "reflection_round": rounds, "next_node": "coordinator"}
