"""Candidate contract checks based on the saved U-MATH pilot failure modes."""

import json
from pathlib import Path

from nodes.arbiter_step import _Candidate, _enforce_contract
from utils.answer_schema import missing_components, required_components


BENCHMARK = Path(__file__).resolve().parents[1] / "benchmarks" / "umath_text24.jsonl"
ROWS = {row["idx"]: row for row in map(json.loads, BENCHMARK.read_text(encoding="utf-8").splitlines())}


def _choose(problem, reasoning_answer, python_answer):
    reasoning = _Candidate("reasoning", reasoning_answer, "")
    python = _Candidate("python", python_answer, "")
    return _enforce_contract(problem, python, [reasoning, python])


def test_indefinite_integral_keeps_constant_of_integration():
    problem = ROWS[8]["problem"]
    reasoning = r"\boxed{-\dfrac{3}{8}\cot^{\frac{8}{3}}x-\dfrac{3}{2}\cot^{\frac{2}{3}}x+C}"
    python = "-3*cot(x)**(8/3)/8 - 3*cot(x)**(2/3)/2"

    selected, override = _choose(problem, reasoning, python)

    assert required_components(problem) == ["integration_constant"]
    assert missing_components(problem, reasoning) == []
    assert missing_components(problem, python) == ["integration_constant"]
    assert selected.source == "reasoning"
    assert selected.answer == reasoning
    assert override["missing_in_selected"] == ["integration_constant"]


def test_definite_integral_does_not_require_constant():
    problem = r"Compute $\int_{0}^{1} x^2\,dx$."
    assert "integration_constant" not in required_components(problem)
    assert missing_components(problem, "1/3") == []


def test_periodic_trig_equation_keeps_full_solution_family():
    problem = ROWS[16]["problem"]
    reasoning = r"x = 2k\pi + \arctan\frac{3}{4},\ k \in \mathbb{Z}"
    python = "x = [2*atan(1/3)]"

    selected, override = _choose(problem, reasoning, python)

    assert required_components(problem) == ["periodic_solution"]
    assert missing_components(problem, reasoning) == []
    assert missing_components(problem, python) == ["periodic_solution"]
    assert selected.source == "reasoning"
    assert override["missing_in_selected"] == ["periodic_solution"]


def test_trig_equation_on_interval_does_not_require_periodic_family():
    row = ROWS[17]
    assert "periodic_solution" not in required_components(row["problem"])
    assert missing_components(row["problem"], row["answer"]) == []


def test_explicit_four_part_problem_prefers_complete_candidate():
    problem = ROWS[12]["problem"]
    complete = ("$R(x,y)=-5x^2-3y^2+2xy+50x+90y$; $R(6,10)=840$; "
                "$P(x,y)=-5x^2-3y^2+2xy+40x+74y-800$; $P(6,10)=-180$")
    partial = "$R(x,y)=-5x^2-3y^2+2xy+50x+90y$; $R(6,10)=840$"

    selected, override = _choose(problem, complete, partial)

    assert required_components(problem) == [
        "field:R(x,y)", "field:R(6,10)", "field:P(x,y)", "field:P(6,10)",
    ]
    assert missing_components(problem, partial) == ["field:P(x,y)", "field:P(6,10)"]
    assert selected.source == "reasoning"
    assert override["missing_in_selected"] == ["field:P(x,y)", "field:P(6,10)"]


def test_named_field_without_a_value_is_missing():
    problem = ROWS[12]["problem"]
    answer = "$R(x,y)=$; $R(6,10)=840$; $P(x,y)=y$; $P(6,10)=-180$"
    assert missing_components(problem, answer) == ["field:R(x,y)"]


def test_contract_does_not_override_when_both_candidates_miss_same_part():
    problem = ROWS[8]["problem"]
    selected, override = _choose(problem, "sin(x)", "cos(x)")
    assert selected.source == "python"
    assert override is None


def test_fewer_missing_fields_without_superset_does_not_override():
    problem = ROWS[12]["problem"]
    selected = _Candidate("python", "$R(x,y)=x$; $R(6,10)=840$", "")
    other = _Candidate("reasoning", "$R(6,10)=840$; $P(x,y)=y$; $P(6,10)=-180$", "")
    chosen, override = _enforce_contract(problem, selected, [other, selected])
    assert chosen is selected
    assert override is None


def test_explicit_contradiction_blocks_syntax_only_override():
    problem = ROWS[8]["problem"]
    reasoning = _Candidate("reasoning", "sin(x)+C", "")
    python = _Candidate("python", "cos(x)", "")
    selected, override = _enforce_contract(
        problem, python, [reasoning, python], unresolved_contradiction=True,
    )
    assert selected is python
    assert override is None


def test_public_references_satisfy_new_contracts():
    assert all(missing_components(row["problem"], row["answer"]) == [] for row in ROWS.values())
