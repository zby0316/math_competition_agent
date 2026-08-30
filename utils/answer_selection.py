"""答案选择：从推理侧与 Python 侧候选里挑出要面世的最终答案。

从 nodes/judge_step.py 抽出，消除 utils -> nodes 的反向导入（层级倒挂）。
node_guard（utils 层）与 judge_step（nodes 层）共用同一套选择逻辑。
"""
from utils.answer_pull import is_multi_part_problem, looks_incomplete_answer
from utils.answer_schema import answer_part_count, missing_components
from utils.cot_trim import is_placeholder_answer
from utils.question_profile import is_objective_mode, normalize_objective_answer, objective_answer_is_usable


def _clean_answer(answer):
    return "" if is_placeholder_answer(answer) else (answer or "")


def _same_scalar_or_text(a, b):
    import re as _re
    ka = _re.sub(r"[\s。．.,，;；]+", "", str(a or ""))
    kb = _re.sub(r"[\s。．.,，;；]+", "", str(b or ""))
    return bool(ka) and ka == kb


def preferred_answer(state, match_result):
    rr = state.get("reasoning_result") or {}
    po = state.get("python_output") or {}
    problem = state.get("problem", "")
    ptype = match_result.get("problem_type", "computation")
    python_answer = _clean_answer(po.get("answer", ""))
    reasoning_answer = _clean_answer(rr.get("answer", ""))
    python_ok = bool(po.get("success")) and bool(python_answer)
    reasoning_ok = bool(reasoning_answer) and not looks_incomplete_answer(reasoning_answer)
    question_mode = state.get("question_mode") or ""
    if is_objective_mode(question_mode):
        for candidate in (reasoning_answer, python_answer):
            normalized = normalize_objective_answer(candidate, question_mode)
            if objective_answer_is_usable(normalized, question_mode):
                return normalized
        return ""
    evidence_status = po.get("evidence_status", "")
    fabricated = bool((po.get("authenticity") or {}).get("fabricated"))
    if evidence_status == "contradict" and python_answer and not fabricated:
        if not looks_incomplete_answer(python_answer):
            if not _same_scalar_or_text(python_answer, reasoning_answer):
                return python_answer
    if match_result.get("status") == "match" and reasoning_ok:
        return reasoning_answer
    if match_result.get("status") == "uncertain" and reasoning_ok:
        if python_ok and not looks_incomplete_answer(python_answer):
            r_missing = missing_components(problem, reasoning_answer)
            p_missing = missing_components(problem, python_answer)
            if len(p_missing) < len(r_missing):
                return python_answer
            if is_multi_part_problem(problem):
                if answer_part_count(python_answer) > answer_part_count(reasoning_answer):
                    return python_answer
        return reasoning_answer
    if ptype == "computation" and python_ok and not looks_incomplete_answer(python_answer):
        if not (fabricated and reasoning_ok):
            return python_answer
    if reasoning_ok:
        return reasoning_answer
    return reasoning_answer or python_answer
