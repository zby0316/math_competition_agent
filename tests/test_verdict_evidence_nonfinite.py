"""Regression checks for undefined SymPy output in the verification branch."""

from nodes.judge_step import judge_step
from utils.verdict_evidence import parse_verification_evidence


SERIES_ANSWER = r"\frac{1}{2}\ln 2"
NAN_STDOUT = (
    "验证状态: FAIL\n"
    "验证证据: Symbolic summation yielded nan, difference from candidate is nan\n"
    "最终答案: nan\n"
)


def test_pilot_series_nan_is_not_a_counterexample_or_answer():
    output = {"success": True, "stdout": NAN_STDOUT, "answer": "nan"}
    evidence = parse_verification_evidence(output, candidate_answer=SERIES_ANSWER)

    assert evidence["answer"] is None
    assert evidence["invalid_answer"] == "nan"
    assert evidence["evidence_status"] == "inconclusive"
    assert evidence["contradictions"] == []
    assert parse_verification_evidence(evidence, SERIES_ANSWER)["answer"] is None

    routed = judge_step(
        {
            "problem": "Find the sum of this series.",
            "reasoning_result": {"answer": SERIES_ANSWER},
            "python_output": output,
            "python_code": "",
            "reconciliation_round": 0,
        },
        {},
    )
    assert routed["validation_status"] == "uncertain_arbitrating"
    assert routed["python_output"]["answer"] is None
    assert routed["python_evidence_status"] == "inconclusive"


def test_finite_fail_remains_a_counterexample():
    output = {
        "success": True,
        "stdout": "验证状态: FAIL\n验证证据: Counterexample x=2: expected 3, got 4\n最终答案: 4\n",
        "answer": "4",
    }
    evidence = parse_verification_evidence(output, candidate_answer="3")

    assert evidence["answer"] == "4"
    assert evidence["evidence_status"] == "contradict"
    assert any("Counterexample x=2: expected 3, got 4" in item
               for item in evidence["contradictions"])


def test_independent_finite_counterexample_survives_nan_diagnostic():
    output = {
        "success": True,
        "stdout": (
            "验证状态: FAIL\n"
            "验证证据: symbolic summation yielded nan\n"
            "反例: x=2 时计算值为 4，候选为 3\n"
            "最终答案: nan\n"
        ),
        "answer": "nan",
    }
    evidence = parse_verification_evidence(output, candidate_answer="3")

    assert evidence["answer"] is None
    assert evidence["evidence_status"] == "contradict"
    assert evidence["contradictions"] == ["反例: x=2 时计算值为 4，候选为 3"]
    assert evidence["evidence_summary"] == evidence["contradictions"][0]


def test_same_line_counterexample_survives_nan_diagnostic():
    output = {
        "success": True,
        "stdout": (
            "验证状态: FAIL\n"
            "验证证据: earlier symbolic result was nan; counterexample x=2: expected 3, got 4\n"
            "最终答案: nan\n"
        ),
        "answer": "nan",
    }
    evidence = parse_verification_evidence(output, candidate_answer="3")

    assert evidence["answer"] is None
    assert evidence["evidence_status"] == "contradict"
    assert any("counterexample x=2: expected 3, got 4" in item
               for item in evidence["contradictions"])


def test_undefined_observation_is_not_a_counterexample():
    output = {
        "success": True,
        "stdout": (
            "验证状态: FAIL\n"
            "验证证据: counterexample x=2: expected 3, got nan\n"
            "最终答案: nan\n"
        ),
        "answer": "nan",
    }
    evidence = parse_verification_evidence(output, candidate_answer="3")

    assert evidence["evidence_status"] == "inconclusive"
    assert evidence["contradictions"] == []


def test_nan_cannot_authorize_pass_evidence():
    output = {"success": True, "stdout": "验证状态: PASS\n最终答案: nan\n", "answer": "nan"}
    evidence = parse_verification_evidence(output, candidate_answer="0")

    assert evidence["evidence_status"] == "inconclusive"
    assert evidence["answer"] is None
