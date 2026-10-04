"""Conservative, offline grading for public math diagnostic sets.

Return True/False only when equivalence or a numeric mismatch is established.
Unparsed answers return None so a formatting difference is not counted as wrong.
"""

from __future__ import annotations

import re

from utils.answer_compare import AnswerCompare, _try_parse_expr
from utils.structured_reply import is_self_conflicting, compare_structured_answers


_FINAL_LABEL_RE = re.compile(r"(?i)^\s*(?:the\s+final\s+answer|final\s+answer|answer|最终答案|答案|结论)\s*[：:]\s*")
_STEP_HEADING_RE = re.compile(r"^\s*(?:#{1,4}\s*)?(?:关键步骤|解题步骤|详细解题步骤|推导步骤)\s*[：:]?\s*$")
_SPLIT_MATH_EQUALS_RE = re.compile(r"\$([^$]+)\$\s*=\s*\$([^$]+)\$", re.DOTALL)
_FUNCTION_LABEL_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*\([^()]*\))\s*=\s*(.+)$", re.DOTALL)
_MINIMUM_RE = re.compile(r"(?i)(?:minimum\s+value|最小值)\s*[：:=]\s*([^;；\n]+)")
_SIMPLE_LABEL_RE = re.compile(r"^\s*[A-Za-z][A-Za-z0-9_]*\s*=\s*(.+)$", re.DOTALL)


def _trim_steps(answer: str) -> str:
    lines = []
    for line in (answer or "").splitlines():
        if _STEP_HEADING_RE.match(line):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _prepare(answer: str) -> str:
    text = _FINAL_LABEL_RE.sub("", _trim_steps(answer).strip())
    # References sometimes split one equation into two adjacent $...$ spans.
    for _ in range(3):
        merged = _SPLIT_MATH_EQUALS_RE.sub(lambda m: f"${m.group(1)}={m.group(2)}$", text)
        if merged == text:
            break
        text = merged
    return text.strip()


def _math_spans(text: str) -> list[str]:
    return [span.strip() for span in re.findall(r"\$([^$]+)\$", text)]


def _function_fields(text: str) -> dict[str, str]:
    fields = {}
    for span in _math_spans(text):
        match = _FUNCTION_LABEL_RE.fullmatch(span)
        if not match:
            continue
        label = re.sub(r"\s+", "", match.group(1))
        value = match.group(2).strip()
        duplicate = re.match(re.escape(match.group(1)) + r"\s*=\s*(.+)$", value, re.DOTALL)
        if duplicate:
            value = duplicate.group(1).strip()
        if label in fields and fields[label] != value:
            return {}
        fields[label] = value
    return fields


def _standalone_expression(text: str) -> str | None:
    value = text.strip()
    if value.startswith(r"\boxed{") and value.endswith("}"):
        value = value[len(r"\boxed{"):-1].strip()
    if value.startswith("$") and value.endswith("$") and value.count("$") == 2:
        value = value[1:-1].strip()
    if "$" in value or any(char in value for char in (";", "；", "\n")):
        return None
    label = _SIMPLE_LABEL_RE.fullmatch(value)
    if label:
        value = label.group(1).strip()
    return value or None


def _equivalent(left: str, right: str, problem: str = "") -> bool | None:
    from utils.domain_constraints import explicit_domains, domain_equivalent
    if explicit_domains(problem):
        return domain_equivalent(problem, left, right)
    return AnswerCompare._sympy_equivalent(left, right, tolerance=1e-6)


def _finite_set(text):
    import sympy as sp
    value = _standalone_expression(text)
    if not value:
        return None
    value = value.replace(r"\{", "{").replace(r"\}", "}")
    if not re.fullmatch(r"\{[^{}]+\}", value):
        return None
    items = [_try_parse_expr(item.strip()) for item in value[1:-1].split(",")]
    if any(item is None or getattr(item, "free_symbols", {1}) or item.is_finite is not True for item in items):
        return None
    return sp.FiniteSet(*items)


def _numeric_mismatch(left: str, right: str) -> bool:
    a, b = _try_parse_expr(left), _try_parse_expr(right)
    return bool(
        a is not None and b is not None
        and not getattr(a, "free_symbols", set())
        and not getattr(b, "free_symbols", set())
        and getattr(a, "is_finite", None) is True
        and getattr(b, "is_finite", None) is True
    )


def grade_math_answer(problem: str, predicted: str, expected: str) -> tuple[bool | None, str]:
    """Grade a result; None means the answer needs human review."""
    if not str(expected or "").strip():
        return None, "review: no_reference"
    if not str(predicted or "").strip():
        return False, "no_answer"

    # Proof grading requires checking the argument, not just its last line.
    if AnswerCompare.detect_problem_type(problem) == "proof":
        return None, "review: proof_requires_argument_check"

    pred, ref = _prepare(predicted), _prepare(expected)
    from utils.question_profile import classify_question_mode
    if classify_question_mode(problem) == "choice":
        if all(re.fullmatch(r"[A-H](?:\s*[,、，/]\s*[A-H])*", text) for text in (pred, ref)):
            equal = set(re.findall(r"[A-H]", pred)) == set(re.findall(r"[A-H]", ref))
            return equal, "choice_labels"
    left_set, right_set = _finite_set(pred), _finite_set(ref)
    if left_set is not None and right_set is not None:
        return bool(left_set == right_set), "finite_numeric_set"
    equivalent = lambda a, b, tolerance=1e-6: _equivalent(a, b, problem)
    # The generic conflict detector mistakes numbered subanswers and alternate
    # trigonometric forms for contradictions. Apply it only to semicolon lists.
    conflicting_list = (";" in pred or "；" in pred) and is_self_conflicting(
        pred, equivalent, 1e-6)
    if re.sub(r"\s+", " ", pred) == re.sub(r"\s+", " ", ref) and not conflicting_list:
        return True, "exact_reference"

    left_fields, right_fields = _function_fields(pred), _function_fields(ref)
    pred_spans, ref_spans = _math_spans(pred), _math_spans(ref)
    if left_fields and right_fields and len(left_fields) == len(pred_spans) and len(right_fields) == len(ref_spans):
        if left_fields.keys() != right_fields.keys():
            return None, "review: different_answer_fields"
        verdicts = [equivalent(left_fields[key], right_fields[key]) for key in left_fields]
        if all(verdict is True for verdict in verdicts):
            return True, "symbolic_function_fields"
        if any(verdict is False for verdict in verdicts):
            return False, "different_function_field"
        return None, "review: unparsed_function_field"

    if len(pred_spans) == len(ref_spans) == 1:
        pred_eq, ref_eq = pred_spans[0].split("="), ref_spans[0].split("=")
        if len(pred_eq) == len(ref_eq) == 2:
            verdicts = [equivalent(a, b) for a, b in zip(pred_eq, ref_eq)]
            if all(verdict is True for verdict in verdicts):
                return True, "symbolic_equation"

    minimum = _MINIMUM_RE.search(pred)
    pred_scalar = minimum.group(1).strip() if minimum else _standalone_expression(pred)
    ref_scalar = _standalone_expression(ref)
    if pred_scalar and ref_scalar:
        verdict = equivalent(pred_scalar, ref_scalar)
        if verdict is True:
            return True, "symbolic_scalar"
        if verdict is False and _numeric_mismatch(pred_scalar, ref_scalar):
            return False, "numeric_mismatch"

    # Retain the existing matcher for clean non-scalar types such as matrices.
    structured = compare_structured_answers(pred, ref, equivalent, tolerance=1e-6)
    if structured.applicable and structured.verdict is not None:
        return structured.verdict, "structured_fields"
    detail = AnswerCompare._match_computation_detailed(pred, ref, problem=problem)
    if detail["verdict"] is True and detail["method"] in {
        "exact", "numeric", "symbolic", "structured_symbolic"
    }:
        return True, detail["reason"]
    return None, "review: " + detail["reason"]
