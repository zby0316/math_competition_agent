"""交叉验证：对比推理与 Python 的结果，据此路由。

加固要点：对计算题，当 Python 成功时优先采用 sympy 算出的答案（确定性优于
LLM 散文，后者可能回显占位符或算错值）。证明题或 python 失败时采用推理侧。
"""
from utils.answer_compare import AnswerCompare
from utils.answer_schema import answer_part_count, missing_components
from utils.answer_pull import is_multi_part_problem, looks_incomplete_answer
from utils.cot_trim import is_placeholder_answer
from utils.reconcile_policy import reconciliation_retry_available
from utils.verdict_evidence import parse_verification_evidence
from utils.question_profile import (
    classify_question_mode,
    fill_answer_matches_blanks,
    is_objective_mode,
    normalize_objective_answer,
    objective_answer_is_usable,
)
from utils.answer_selection import preferred_answer as _preferred_answer


def judge_step(state, config):
    reasoning_result = state.get("reasoning_result") or {}
    raw_python_output = state.get("python_output") or {}
    python_output = parse_verification_evidence(
        raw_python_output,
        candidate_answer=reasoning_result.get("answer", ""),
        code=state.get("python_code", ""),
    )
    question_mode = state.get("question_mode") or classify_question_mode(state.get("problem", ""))
    if is_objective_mode(question_mode):
        # 客观题刻意没有 Python 候选。来自精简推理路径的、规范化的有效答案才是
        # 唯一值得输出的候选；把它送进计算匹配器，会仅因 Python 被跳过而标成
        # uncertain，并触发不必要的仲裁/对账轮。
        candidate = normalize_objective_answer(reasoning_result.get("answer", ""), question_mode)
        if not objective_answer_is_usable(candidate, question_mode):
            candidate = normalize_objective_answer(python_output.get("answer", ""), question_mode)
        if objective_answer_is_usable(candidate, question_mode):
            blank_gap = question_mode == "fill" and not fill_answer_matches_blanks(
                candidate, state.get("problem", ""))
            match_result = {
                "status": "match",
                "verdict": True,
                "comparison_verdict": True,
                # 填空分项数不足题面空位数时降低置信（残缺答案曾以 0.78 置信直接
                # 放行，无任何完整性检查）。
                "confidence": 0.45 if blank_gap else 0.78,
                "reason": f"客观题快速路径已提取{question_mode}答案"
                          + ("；但分项数少于题面空位数，答案可能不完整" if blank_gap else ""),
                "method": "objective_direct",
                "problem_type": question_mode,
                "text_similarity": 1.0,
                "matched_fields": [],
                "mismatched_fields": [],
                "field_coverage": 0.5 if blank_gap else 1.0,
            }
        else:
            match_result = {
                "status": "uncertain",
                "verdict": None,
                "comparison_verdict": None,
                "confidence": 0.0,
                "reason": "客观题未提取到可提交的答案",
                "method": "objective_unparsed",
                "problem_type": question_mode,
                "text_similarity": 0.0,
                "matched_fields": [],
                "mismatched_fields": [],
                "field_coverage": 0.0,
            }
    else:
        match_result = dict(AnswerCompare.match_answers(
            problem=state["problem"],
            reasoning_result=reasoning_result,
            python_result=python_output,
        ))
    evidence_status = python_output.get("evidence_status", "inconclusive")
    evidence_summary = python_output.get("evidence_summary", "")
    contradictions = list(python_output.get("contradictions") or [])
    match_result.update({
        "python_evidence_status": evidence_status,
        "python_evidence_summary": evidence_summary,
        "python_contradictions": contradictions,
    })
    status = match_result["status"]
    validated_answer = ""
    next_node = "coordinator"
    problem_type = match_result.get("problem_type", "computation")

    history_entry = {
        "round": state.get("reconciliation_round", 0),
        "status": status,
        "evidence_status": evidence_status,
        "evidence_summary": evidence_summary[:1000],
        "contradictions": contradictions[:10],
    }

    # 反例的显式证据优先于文本/符号上的一致。程序可能运行成功，同时恰恰证明
    # 该候选是错的。
    if evidence_status == "contradict":
        match_result["routing_reason"] = "python_evidence_contradiction"
        if reconciliation_retry_available(state, config):
            status = "mismatch_reconciling"
            next_node = "reconciliation"
        else:
            status = "mismatch_arbitrating"
            next_node = "arbiter"
            match_result["unresolved_contradiction"] = True
        history_entry["status"] = status
        return {
            "validation_status": status,
            "validation_details": match_result,
            "validated_answer": validated_answer,
            "next_node": next_node,
            "python_output": python_output,
            "python_evidence_status": evidence_status,
            "python_evidence_summary": evidence_summary,
            "python_contradictions": contradictions,
            "validation_history": [history_entry],
        }

    if status == "match":
        if problem_type == "computation" and python_output.get("success") \
                and match_result.get("method") in {"symbolic", "structured_symbolic"}:
            match_result["bypass_reason"] = "python_success_and_symbolic_equivalence"
        match_result["routing_reason"] = "validated_match"
        if match_result.get("method") == "objective_direct":
            validated_answer = normalize_objective_answer(
                reasoning_result.get("answer", ""), question_mode
            )
            if not validated_answer:
                validated_answer = normalize_objective_answer(
                    python_output.get("answer", ""), question_mode
                )
        else:
            validated_answer = _preferred_answer(state, match_result)
        next_node = "coordinator"
    elif status == "mismatch":
        # 子图级重试由 reconciliation_round 门控（而非按节点的 attempts——按节点的
        # attempts 门控的是智能体内部格式/代码重试；这两者在设计中保持分离）。
        if reconciliation_retry_available(state, config):
            status = "mismatch_reconciling"
            next_node = "reconciliation"
        else:
            status = "mismatch_arbitrating"
            match_result["routing_reason"] = "mismatch_retry_exhausted_arbitration"
            next_node = "arbiter"
    else:  # uncertain
        # 解析/格式上的不确定正是关键词契约最薄弱之处。在付出又一次完整
        # 推理+Python 运行之前，让语义选择器在两个既有答案之间做裁决。仲裁器可能
        # 弃权并回到对账，而不改动任何一个候选。
        status = "uncertain_arbitrating"
        match_result["routing_reason"] = "insufficient_equivalence_evidence_arbitration"
        next_node = "arbiter"

    history_entry["status"] = status
    # V2.5 合并：确定性求解器兜底——双路都未产出可用答案时，det 命中即用
    # （确定性符号计算 > LLM 散文，且零额外调用成本）。
    if not validated_answer and state.get("det_answer"):
        validated_answer = str(state.get("det_answer")).strip()
        match_result["routing_reason"] = "det_solver_fallback"
    return {
        "validation_status": status,
        "validation_details": match_result,
        "validated_answer": validated_answer,
        "next_node": next_node,
        "python_output": python_output,
        "python_evidence_status": evidence_status,
        "python_evidence_summary": evidence_summary,
        "python_contradictions": contradictions,
        "validation_history": [history_entry],
    }
