"""Offline regressions from the UGMathBench six-question pilot."""

import json
from pathlib import Path

from nodes.classify_domain import _domain_override
from nodes.reason_step import _parse_reasoning_output
from utils.answer_sanitize import is_junk
from utils.cot_trim import is_placeholder_answer


DATASET = Path(__file__).resolve().parents[1] / "benchmarks" / "ugmath_hard26.jsonl"
ROWS = {row["idx"]: row for row in map(json.loads, DATASET.read_text(encoding="utf-8").splitlines())}


def test_pde_marker_beats_fourier_transform_analysis_cue():
    problem = ROWS[12]["problem"]
    assert _domain_override(problem, ["偏微分方程", "数学分析"]) == "偏微分方程"
    assert _domain_override("This PDE uses a Fourier transform.", ["偏微分方程", "数学分析"]) == "偏微分方程"
    assert _domain_override("Use the Fourier transform of f.", ["偏微分方程", "数学分析"]) == "数学分析"


def test_final_answer_cross_reference_falls_back_to_boxed_conclusion():
    formula = r"u(x,t)=\frac{8}{\sqrt{1+t}}e^{-\frac{(x-6t)^2}{4(1+t)}}"
    response = f"## 结论速览\n\\boxed{{{formula}}}\n\n## 最终答案\nSame as 结论速览."
    parsed = _parse_reasoning_output(response)

    assert is_placeholder_answer("Same as 结论速览.")
    assert not is_placeholder_answer("same as x=2")
    assert parsed["answer_source"] == "quick_conclusion"
    assert "Same as" not in parsed["answer"]
    assert r"\sqrt{1+t}" in parsed["answer"]


def test_multiple_function_fields_are_not_mistaken_for_prose():
    answer = "; ".join(
        f"$A({m},{n})={value}$"
        for m, n, value in ((2, 3, 16), (2, 0, 0), (2, 1, 2), (2, 2, 4), (1, 0, 0), (3, 3, 65536))
    )
    assert not is_junk(answer)
    assert is_junk("We need to try another method. Maybe this is wrong.")
