"""Regression for the Round 4 PDE recovery failure.

A verifier that prints ``验证状态: FAIL`` may still print a proposed formula.
That formula is diagnostic output, not a verified answer, and must never replace
the reasoning branch or enter semantic arbitration.
"""

from nodes.arbiter_step import _candidate_pool
from nodes.assemble_step import _evidence_override
from utils.answer_selection import preferred_answer
from utils.verdict_evidence import parse_verification_evidence


FAIL_OUTPUT = {
    "success": True,
    "answer": "8*exp(-(x-6*t)**2/(16*t+4))/sqrt(4*t+1)",
    "stdout": (
        "验证状态: FAIL\n"
        "验证证据: PDE residual: x**2 - 2 is nonzero\n"
        "最终答案: 8*exp(-(x-6*t)**2/(16*t+4))/sqrt(4*t+1)\n"
    ),
}


def test_failed_verifier_answer_is_diagnostic_only():
    reasoning = "8/sqrt(1+t)*exp(-(x-6*t)**2/(4*(1+t)))"
    python = parse_verification_evidence(FAIL_OUTPUT, candidate_answer=reasoning)

    assert python["evidence_status"] == "contradict"
    assert python["verification_marker"] == "FAIL"

    state = {
        "problem": "Solve the PDE.",
        "reasoning_result": {"answer": reasoning},
        "python_output": python,
    }
    assert preferred_answer(state, {"problem_type": "computation", "status": "mismatch"}) == reasoning
    assert _evidence_override(state, reasoning) == (reasoning, "python_self_verification_failed")
    assert [candidate.source for candidate in _candidate_pool(state)] == ["reasoning"]
