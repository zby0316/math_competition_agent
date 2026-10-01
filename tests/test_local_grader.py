"""Offline checks for the public benchmark grader's conservative decisions."""

import json
from pathlib import Path

import pytest

from eval_local import grade
from utils.local_grader import grade_math_answer


DATASET = Path(__file__).resolve().parents[1] / "benchmarks" / "umath_pilot6.jsonl"
ROWS = {row["idx"]: row for row in map(json.loads, DATASET.read_text(encoding="utf-8").splitlines())}


@pytest.mark.parametrize(
    ("idx", "response", "verdict"),
    [
        (0, r"最终答案：$x^2+\frac{2}{3}x-\frac{1}{3}=\left(x+\frac{1}{3}\right)^2-\frac{4}{9}$", True),
        (4, "最终答案：Range = [1/2 + sqrt(5)/2, 2]; Minimum value = 1/2 + sqrt(5)/2", True),
        (8, r"最终答案：\boxed{-\dfrac38\cot^{8/3}x-\dfrac32\cot^{2/3}x+C}", None),
        (12, ("最终答案：$R(x,y)=-5x^2-3y^2+2xy+50x+90y$；"
              "$R(6,10)=840$；$P(x,y)=-5x^2-3y^2+2xy+40x+74y-800$；"
              "$P(6,10)=-180$"), True),
        (16, r"最终答案：x=2k\pi+\arctan\frac34,\ k\in\mathbb{Z}", None),
        (20, r"最终答案：\frac{1}{2}\ln 2", True),
    ],
)
def test_pilot_cases(idx, response, verdict):
    row = ROWS[idx]
    assert grade(row["problem"], response, row["answer"])[0] is verdict


def test_gold_answers_match_themselves():
    rows = (json.loads(line) for line in (DATASET.parent / "umath_text24.jsonl").read_text(encoding="utf-8").splitlines())
    assert all(grade_math_answer(row["problem"], row["answer"], row["answer"])[0] is True for row in rows)


def test_clear_wrong_numeric_answer_is_false():
    assert grade_math_answer("Find the sum", "3", "4") == (False, "numeric_mismatch")


def test_wrong_function_field_is_false():
    row = ROWS[12]
    predicted = ("$R(x,y)=-5x^2-3y^2+2xy+50x+90y$；$R(6,10)=840$；"
                 "$P(x,y)=-5x^2-3y^2+2xy+40x+74y-800$；$P(6,10)=-181$")
    assert grade_math_answer(row["problem"], predicted, row["answer"])[0] is False


def test_missing_function_field_needs_review():
    row = ROWS[12]
    assert grade_math_answer(row["problem"], "$R(6,10)=840$", row["answer"])[0] is None


def test_extra_conflicting_field_needs_review():
    row = ROWS[12]
    predicted = row["answer"] + "\n$P(6,10)=-181$"
    assert grade_math_answer(row["problem"], predicted, row["answer"])[0] is None


def test_proof_requires_review_even_with_matching_text():
    assert grade_math_answer("Prove that 1+1=2", "1+1=2", "1+1=2")[0] is None


def test_matching_but_self_conflicting_answer_is_not_confirmed():
    assert grade_math_answer("Find a", "a=1; a=2", "a=1; a=2")[0] is None


def test_missing_inputs():
    assert grade_math_answer("Find x", "3", "")[0] is None
    assert grade_math_answer("Find x", "", "3")[0] is False
