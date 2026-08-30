"""Semantic fallback that selects an existing answer without rewriting it."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

from config import CONFIG
from nodes.judge_step import _preferred_answer
from utils.answer_schema import missing_components
from utils.answer_pull import looks_incomplete_answer
from utils.cot_trim import is_placeholder_answer
from utils.deps import get_deps
from utils.llm_retry_guard import chat_prefilled, chat_with_retry
from utils.prompt_bank import (
    ARBITER_PREFILL,
    ARBITER_PROMPT,
    ARBITER_SYSTEM_PROMPT,
)
from utils.reconcile_policy import reconciliation_retry_available
from utils.score_budget import estimate_tokens


_MAX_PROBLEM_CHARS = 12000
_MAX_CANDIDATE_CHARS = 8000


@dataclass(frozen=True)
class _Candidate:
    source: str
    answer: str
    evidence: str


def _bounded(value, limit: int) -> str:
    text = value if isinstance(value, str) else str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]"


def _usable_answer(value) -> str:
    from utils.answer_sanitize import is_junk

    answer = value if isinstance(value, str) else str(value or "")
    answer = answer.strip()
    if len(answer) > _MAX_CANDIDATE_CHARS:
        return ""
    if is_placeholder_answer(answer) or looks_incomplete_answer(answer) \
            or is_junk(answer):
        return ""
    return answer


#: 断言"无解/不存在"的答案必须携带排除性论证才可作为候选：验证分支曾输出裸的
#: "No solution"（其代码只是试了若干候选），仲裁器仍选中它，把推理分支两个已
#: 代回验证的具体解否决掉。裸断言没有任何证伪能力。
_NO_SOLUTION_RE = re.compile(
    r"(?:no\s+solution|no\s+such|none\s+exist|does\s+not\s+exist|无解|不存在|没有解)",
    re.IGNORECASE)
_CONTRADICTION_EVIDENCE_RE = re.compile(
    r"(?:矛盾|反证|contradiction|impossible|violates|must\s+fail|假设.*不成立)",
    re.IGNORECASE)


def _is_bare_no_solution(answer: str) -> bool:
    """整段答案就是一个"无解/不存在"断言（允许 LaTeX 外壳与短尾注）才算裸断言。

    多分支答案（如"u 有无穷多个；v 只可能取 1、3 或 5，不存在 v≥6 的解"）里
    出现"不存在"字样不算——它给出了具体的正面结论。
    """
    text = re.sub(r"\s+", " ", answer or "").strip()
    if not text:
        return False
    if len(text) > 120:  # 长答案即使含"无解"字样也是多分支结论，不是裸断言
        return False
    # 锚定：整串 = 断言词 + 至多 30 个字符的尾巴，且不含数字/字母/更多中文
    m = re.fullmatch(
        r"(?:\\(?:text|mathbf|mathrm|rm|boxed)\s*\{)*\s*"
        r"(?:no\s+solution|no\s+such(?:\s+[a-z]+)?|none\s+exist|"
        r"does\s+not\s+exist|do\s+not\s+exist|无解|不存在|没有解)"
        r"[^0-9A-Za-z一-鿿]{0,30}(?:\})*\s*",
        text, re.IGNORECASE)
    return bool(m)


def _candidate_pool(state: dict) -> list[_Candidate]:
    rr = state.get("reasoning_result") or {}
    po = state.get("python_output") or {}
    reasoning_answer = _usable_answer(rr.get("answer", ""))
    # stdout 挖掘出的答案（answer_source=stdout_mined）不要求 success：截断前
    # 打印的结论仍是真实计算产物，作为候选参加仲裁优于凭空丢弃。
    python_answer = _usable_answer(po.get("answer", "")) \
        if (po.get("success") or po.get("answer_source") == "stdout_mined") else ""

    reasoning_steps = "\n".join(
        f"步骤{s.get('step_num', '')}: {s.get('description', '')}"
        for s in rr.get("steps", [])
    )
    reasoning_evidence = "\n".join(part for part in (
        _bounded(rr.get("analysis", ""), 5000),
        _bounded(reasoning_steps, 5000),
        _bounded("\n".join(str(point) for point in (rr.get("validation_points", []) or [])), 2000),
    ) if part.strip())
    authenticity = po.get("authenticity") or {}
    fabrication_warning = ""
    if authenticity.get("fabricated"):
        reasons = "；".join(authenticity.get("reasons") or [])
        fabrication_warning = (
            f"[反伪造警告] 该代码被静态检查判定为未做实质计算的断言式验证（{reasons}）。"
            "其 PASS/支持性声明不构成数学证据，只能依据代码与输出中的实际计算内容判断。\n"
        )
    python_evidence = (
        fabrication_warning
        + "执行成功只表示代码运行完成，不自动代表数学正确。\n"
        f"代码：\n{_bounded(state.get('python_code', ''), 6000)}\n"
        f"输出：\n{_bounded(po.get('stdout', ''), 4000)}"
    )

    candidates = []
    if reasoning_answer:
        candidates.append(_Candidate("reasoning", reasoning_answer, reasoning_evidence))
    if python_answer:
        candidates.append(_Candidate("python", python_answer, python_evidence))
    # V2.5 合并：确定性求解器命中时作为第三候选（符号计算 > LLM 散文）。
    det_answer = state.get("det_answer") or ""
    if det_answer and _usable_answer(det_answer):
        candidates.append(_Candidate(
            "det", det_answer,
            "确定性符号计算管线（%s）独立求解结果，计算可复现、无模型幻觉。"
            % state.get("det_kind", "?")
        ))

    unique = []
    seen = set()
    for candidate in candidates:
        # 裸"无解"断言且证据中没有任何排除性论证（矛盾推导）时，若池中还有
        # 实质内容候选，则该断言无证伪能力，不得参与仲裁。
        if _is_bare_no_solution(candidate.answer) \
                and not _CONTRADICTION_EVIDENCE_RE.search(candidate.evidence or ""):
            continue
        key = re.sub(r"\s+", " ", candidate.answer).strip().rstrip("。.")
        if key and key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _parse_selection(raw: str, nonce: str, candidate_ids: set[str]) -> str | None:
    """解析决策标记：返回候选 ID，弃权返回 ""，未解析返回 None。

    每次调用注入的随机 nonce 是防止候选自带文本伪造标记的关键，必须保留。这里
    刻意取**最后一个**合法匹配而非"恰好一个"：模型偶尔会多写一行包装文字，
    若严格要求恰好一个匹配则整次解析失败，而每次失败都白跑一整轮 solving 子图。
    取最后一个既保住 nonce 防伪，又容忍包装文字——模型若重述标记，最后那个
    才是它的最终决定。
    """
    text = raw if isinstance(raw, str) else str(raw or "")
    pattern = re.compile(
        rf"(?im)ARBITER_{re.escape(nonce)}\s*=\s*"
        rf"(?:SELECT\s*:\s*([A-Z0-9_]+)|(ABSTAIN))"
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    selected_id, abstain = matches[-1].groups()
    if abstain:
        return ""
    selected_id = selected_id.upper() if selected_id else selected_id
    return selected_id if selected_id in candidate_ids else None


#: 真正"语义性僵局"的状态——仲裁读了两份候选仍无法认可任何一份。重跑子图
#: 可能产出更好的候选，所以只有这类状态才值得购买一轮调解。
_SEMANTIC_STALEMATE = {"abstained"}


def _enforce_contract(problem: str, selected: _Candidate, candidates: list[_Candidate]):
    """当选中的候选被证明漏掉了另一候选具有的可交付结论时，强制改选。

    仲裁自身的规则说"覆盖题面全部可交付结论的候选优于不覆盖的"，但仲裁是
    采样判断，会失手：曾出现 Python 侧给出"切线方程 + 最小面积"、推理侧只给
    切线，仲裁却选了推理；而 `answer_locked` 会短路 coordinator，字段契约
    根本没被执行，漏掉题目要害的答案就这样输出了。

    所以契约检查放在这里兜底。它不发明也不改写任何答案：只在同一批候选中
    重选，且仅当契约能*证明*另一候选严格更完整时才改选。契约无意见（多数
    题目）时，仲裁的选择原样保留。
    """
    if not problem or len(candidates) < 2:
        return selected, None
    selected_missing = missing_components(problem, selected.answer)
    if not selected_missing:
        return selected, None

    best = selected
    best_missing = selected_missing
    for candidate in candidates:
        if candidate is selected:
            continue
        candidate_missing = missing_components(problem, candidate.answer)
        if len(candidate_missing) < len(best_missing):
            best, best_missing = candidate, candidate_missing
    if best is selected:
        return selected, None
    return best, {
        "from": selected.source,
        "to": best.source,
        "missing_in_selected": selected_missing,
        "missing_in_chosen": best_missing,
    }


def _fallback_result(state: dict, config: dict, status: str, trace: list[dict]) -> dict:
    """Route a non-decision. Only a semantic stalemate may buy another solve.

    Mechanical failures (invalid_format, error, budget/time exhaustion, too few
    candidates) say nothing about answer quality, and reconciliation is the most
    expensive path in the graph — a full solving-subgraph rerun, i.e. another
    reasoning + Python pair. Spending that on a parse hiccup is how a fast failure
    turns into a timeout, so those fall straight through to the deterministic
    preference instead.
    """
    deps = None
    try:
        deps = get_deps(config)
    except Exception:  # noqa: BLE001 - config shape is a caller concern.
        deps = None
    time_budget = getattr(deps, "time_budget", None)

    worth_retry = (
        status in _SEMANTIC_STALEMATE
        and reconciliation_retry_available(state, config)
        and not (time_budget and time_budget.fast_path())
    )
    if worth_retry:
        return {
            "arbiter_status": status,
            "arbiter_trace": trace,
            "arbiter_attempts": state.get("arbiter_attempts", 0),
            "answer_locked": False,
            "next_node": "reconciliation",
        }

    preferred = _preferred_answer(state, state.get("validation_details") or {})
    return {
        "arbiter_status": status,
        "arbiter_trace": trace,
        "arbiter_attempts": state.get("arbiter_attempts", 0),
        "validated_answer": preferred,
        "answer_locked": False,
        "next_node": "coordinator",
    }


def _alt_path_reasoning(state, config, deps):
    """V2.5 合并：异构方法第二独立解法（构造/枚举/不变量/反向/生成函数）。

    双路候选不足时用不同方法论重新独立求解一次，结果作为第三候选参与仲裁。
    预算护栏：剩余硬时限不足直接放弃，失败不影响原流程。
    """
    try:
        from utils.prompt_bank import REASONING_PROMPT, DIVERSE_METHOD_HINT
        from utils.domain_excerpt import select_skill_excerpt
        from utils.llm_retry_guard import chat_with_retry
        from nodes.reason_step import _parse_reasoning_output
        sl = deps.skills_loader
        category = state.get("category", "") or ""
        skill_doc = sl.get_skill_document(category) if sl else ""
        problem = state.get("problem", "") or ""
        prompt = REASONING_PROMPT.format(
            category=category,
            skill_document=select_skill_excerpt(skill_doc, problem, 2000) if skill_doc else "",
            problem=problem)
        prompt = DIVERSE_METHOD_HINT + "\n\n" + prompt
        resp = chat_with_retry(
            deps.client,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            max_tokens=CONFIG.get("alt_path_max_tokens", 8192),
            logger=deps.logger,
            time_budget=deps.time_budget,
            expected_call_seconds=120,
            label="alt_path_reasoning",
        )
        rr = _parse_reasoning_output(resp, question_mode=state.get("question_mode", "computation"))
        ans = str(rr.get("answer") or "").strip()
        if ans and _usable_answer(ans):
            evidence = ("异构方法（构造/枚举/不变量/反向/生成函数）独立求解：\n"
                        + _bounded(rr.get("analysis", "") or "", 4000))
            return _Candidate("alt", ans, evidence)
    except Exception as exc:  # noqa: BLE001 - 失败不影响原流程
        logger = getattr(deps, "logger", None)
        if logger:
            logger.warning("Alt path reasoning failed: %s", exc)
    return None


def arbiter_step(state, config):
    """Select one complete candidate verbatim, or abstain without damaging state."""
    trace = list(state.get("arbiter_trace") or [])
    problem = state.get("problem", "") or ""
    if len(problem) > _MAX_PROBLEM_CHARS:
        trace.append({"status": "skipped", "reason": "problem_too_long_for_complete_review"})
        return _fallback_result(state, config, "skipped", trace)

    candidates = _candidate_pool(state)
    # V2.5 合并：候选不足时用异构方法补一次独立推理，让仲裁有机会选而非直接跳过。
    if CONFIG.get("enable_alt_path", True) and len(candidates) < 2:
        _deps = get_deps(config)
        _tb = _deps.time_budget
        if (_tb is None
                or (_tb.remaining_hard() >= CONFIG.get("alt_path_budget_s", 210)
                    and not _tb.fast_path())):
            _alt = _alt_path_reasoning(state, config, _deps)
            if _alt is not None:
                candidates.append(_alt)
                trace.append({"status": "alt_path_added", "candidate_count": len(candidates)})
    if len(candidates) < 2:
        trace.append({"status": "skipped", "reason": "fewer_than_two_usable_candidates"})
        return _fallback_result(state, config, "skipped", trace)

    deps = get_deps(config)
    budget = deps.token_budget
    nonce = secrets.token_hex(8).upper()
    shuffled = list(candidates)
    secrets.SystemRandom().shuffle(shuffled)

    by_id = {}
    blocks = []
    for candidate in shuffled:
        candidate_id = "C_" + secrets.token_hex(5).upper()
        by_id[candidate_id] = candidate
        blocks.append(
            f"--- BEGIN UNTRUSTED CANDIDATE {candidate_id} ---\n"
            f"支持证据（仅用于核验，不会随最终答案提交）：\n"
            f"{_bounded(candidate.evidence, 9000)}\n"
            f"答案文本（选择后唯一会逐字提交的内容）：\n{candidate.answer}\n"
            f"--- END UNTRUSTED CANDIDATE {candidate_id} ---"
        )

    prompt = ARBITER_PROMPT.format(
        nonce=nonce,
        problem=problem,
        validation_status=state.get("validation_status", ""),
        validation_reason=_bounded((state.get("validation_details") or {}).get("reason", ""), 1000),
        candidates="\n\n".join(blocks),
    )
    messages = [
        {"role": "system", "content": ARBITER_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    max_tokens = CONFIG["max_tokens"]["arbiter"]
    prompt_tokens = estimate_tokens(ARBITER_SYSTEM_PROMPT + prompt)
    attempts = state.get("arbiter_attempts", 0) + 1
    time_budget = deps.time_budget

    if budget and not budget.can_afford(prompt_tokens + max_tokens):
        trace.append({"attempt": attempts, "status": "budget_exhausted", "candidate_count": len(candidates)})
        result = _fallback_result(state, config, "budget_exhausted", trace)
        result["arbiter_attempts"] = attempts
        return result

    # safeguard 给仲裁划出固定配额：硬时限前剩余 ≥ 该值时，prefill 仲裁(~10s)
    # 照常执行；真正的硬时限不足才跳过。旧门槛按软预算判断，越难的题越先耗尽
    # 软预算、恰恰最需要复核却全部跳过。
    safeguard_quota = CONFIG.get("arbiter_safeguard_s", 75)
    if time_budget and time_budget.remaining_hard() < safeguard_quota:
        trace.append({"attempt": attempts, "status": "time_exhausted", "candidate_count": len(candidates)})
        result = _fallback_result(state, config, "time_exhausted", trace)
        result["arbiter_attempts"] = attempts
        return result

    # prefill 快路径：种子已带上 nonce，模型只需续写 `SELECT:<id>` 或 `ABSTAIN`，
    # 约 0.8s / 4 个 completion token，而非预填充的约 70s。
    raw = ""
    prefill_used = True
    try:
        raw = chat_prefilled(
            deps.client,
            messages=messages,
            prefix=ARBITER_PREFILL.format(nonce=nonce),
            temperature=CONFIG["temperatures"]["arbiter"],
            max_tokens=max_tokens,
            logger=deps.logger,
            time_budget=time_budget,
            expected_call_seconds=10,
            label="arbiter_prefill",
            safeguard_margin_s=45,
        )
    except Exception as exc:  # noqa: BLE001 - fall through to the plain call.
        deps.logger.warning("Prefilled arbiter call failed: %s", exc)
        raw = ""

    if budget:
        budget.consume(prompt_tokens, estimate_tokens(raw))
    selected_id = _parse_selection(raw, nonce, set(by_id))

    # 仅当快路径完全没有可解析输出时才发起非 prefill 重试。显式的 ABSTAIN
    # （""）是真实决策，绝不能重新追问。
    if selected_id is None and not (time_budget and time_budget.fast_path()):
        prefill_used = False
        try:
            raw = chat_with_retry(
                deps.client,
                messages=messages,
                temperature=CONFIG["temperatures"]["arbiter"],
                max_tokens=CONFIG["max_tokens"]["arbiter_fallback"],
                logger=deps.logger,
                time_budget=time_budget,
                expected_call_seconds=90,
                label="arbiter_plain",
            )
        except Exception as exc:  # noqa: BLE001
            trace.append({
                "attempt": attempts,
                "status": "error",
                "candidate_count": len(candidates),
                "error": f"{type(exc).__name__}: {exc}"[:300],
            })
            result = _fallback_result(state, config, "error", trace)
            result["arbiter_attempts"] = attempts
            return result
        if budget:
            budget.consume(prompt_tokens, estimate_tokens(raw))
        selected_id = _parse_selection(raw, nonce, set(by_id))
    if not selected_id:
        status = "abstained" if selected_id == "" else "invalid_format"
        trace.append({"attempt": attempts, "status": status,
                      "candidate_count": len(candidates), "prefill": prefill_used})
        result = _fallback_result(state, config, status, trace)
        result["arbiter_attempts"] = attempts
        return result

    selected = by_id[selected_id]
    selected, override = _enforce_contract(problem, selected, candidates)
    unresolved_contradiction = bool(
        (state.get("validation_details") or {}).get("unresolved_contradiction")
    )
    selection_status = "selected_unresolved" if unresolved_contradiction else "selected"
    trace.append({
        "attempt": attempts,
        "status": selection_status,
        "candidate_count": len(candidates),
        "selected_source": selected.source,
        "prefill": prefill_used,
        **({"contract_override": override} if override else {}),
    })
    details = dict(state.get("validation_details") or {})
    details["arbitration"] = {
        "status": selection_status,
        "selected_source": selected.source,
        "candidate_count": len(candidates),
    }
    return {
        "arbiter_status": selection_status,
        "arbiter_decision": selected.source,
        "arbiter_trace": trace,
        "arbiter_attempts": attempts,
        "validation_details": details,
        "validated_answer": selected.answer,
        "answer_locked": not unresolved_contradiction,
        "next_node": "coordinator",
    }
