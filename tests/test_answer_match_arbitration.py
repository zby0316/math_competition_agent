"""Offline routing checks for equivalent U-MATH pilot answers."""

import json
from pathlib import Path

import pytest

from nodes.judge_step import judge_step
from utils.answer_compare import AnswerCompare


DATASET = Path(__file__).resolve().parents[1] / "benchmarks" / "umath_pilot6.jsonl"
ROWS = {row["idx"]: row for row in map(json.loads, DATASET.read_text(encoding="utf-8").splitlines())}

PILOT_ANSWERS = {
    0: (
        r"$x^2 + \frac{2}{3}x - \frac{1}{3} = \left(x + \frac{1}{3}\right)^2 - \frac{4}{9}$",
        "(3*x + 1)**2/9 - 4/9",
    ),
    4: (
        r"函数的最小值为 $\boxed{\frac{1+\sqrt{5}}{2}}$",
        "Range = [1/2 + sqrt(5)/2, 2]; Minimum value = 1/2 + sqrt(5)/2",
    ),
    8: (
        r"\boxed{-\dfrac{3}{8}\cot^{\frac{8}{3}}x-\dfrac{3}{2}\cot^{\frac{2}{3}}x+C}",
        "-3*cot(x)**(8/3)/8 - 3*cot(x)**(2/3)/2",
    ),
    12: (
        "1. 收益函数：$R(x,y)=-5x^2-3y^2+2xy+50x+90y$；"
        "2. 收益值：$R(6,10)=840$；"
        "3. 利润函数：$P(x,y)=-5x^2-3y^2+2xy+40x+74y-800$；"
        "4. 利润值：$P(6,10)=-180$",
        "R(x,y)=-5*x**2+2*x*y+50*x-3*y**2+90*y; R(6,10)=840; "
        "P(x,y)=-5*x**2+2*x*y+40*x-3*y**2+74*y-800; P(6,10)=-180",
    ),
    16: (
        r"x=2k\pi+\arctan\frac{3}{4},\ k\in\mathbb{Z}",
        "x=[2*atan(1/3)]",
    ),
}


def _route(idx, reasoning=None, python=None):
    original_reasoning, original_python = PILOT_ANSWERS[idx]
    reasoning = original_reasoning if reasoning is None else reasoning
    python = original_python if python is None else python
    return judge_step(
        {
            "problem": ROWS[idx]["problem"],
            "reasoning_result": {"answer": reasoning},
            "python_output": {
                "success": True,
                "answer": python,
                "stdout": "验证状态: PASS\n最终答案: " + python,
            },
            "python_code": "",
            "reconciliation_round": 0,
        },
        {},
    )


@pytest.mark.parametrize("idx", [0, 4, 12])
def test_demonstrated_equivalence_skips_arbitration(idx):
    result = _route(idx)
    assert result["validation_status"] == "match"
    assert result["next_node"] == "coordinator"
    assert result["validated_answer"] == PILOT_ANSWERS[idx][0]


@pytest.mark.parametrize("idx", [8, 16])
def test_different_completeness_still_needs_arbitration(idx):
    result = _route(idx)
    assert result["validation_status"] == "uncertain_arbitrating"
    assert result["next_node"] == "arbiter"


def test_identity_must_really_be_an_identity():
    detail = AnswerCompare._match_computation_detailed("$x^2=4$", "4")
    assert detail["verdict"] is not True


def test_wrong_completed_square_is_not_matched():
    result = _route(0, python="(3*x + 1)**2/9 - 5/9")
    assert result["validation_status"] != "match"


def test_wrong_minimum_does_not_skip_arbitration():
    result = _route(4, python="Range = [1, 2]; Minimum value = 2")
    assert result["validation_status"] != "match"


def test_extra_requested_quantity_blocks_minimum_projection():
    problem = ROWS[4]["problem"].replace("Find the minimum value", "Find the minimum value and maximum value")
    reasoning, python = PILOT_ANSWERS[4]
    assert not AnswerCompare._minimum_answers_match(problem, reasoning, python, 1e-6)


def test_wrong_named_field_is_a_mismatch():
    _, python = PILOT_ANSWERS[12]
    result = _route(12, python=python.replace("P(6,10)=-180", "P(6,10)=-181"))
    assert result["validation_status"] != "match"
    assert "p(6,10)" in result["validation_details"]["mismatched_fields"]


def test_missing_named_field_does_not_look_complete():
    _, python = PILOT_ANSWERS[12]
    result = _route(12, python=python.split("; P(6,10)")[0])
    assert result["validation_status"] != "match"
