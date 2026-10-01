"""Proof-domain routing regressions, including neighboring-domain boundaries."""

import json
from pathlib import Path

import pytest

from nodes.classify_domain import _domain_override


ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = [p.name for p in (ROOT / "knowledge").iterdir() if p.is_dir()]


def test_closed_graph_counterexample_loads_functional_domain():
    rows = [json.loads(line) for line in (ROOT / "benchmarks" / "proof_gap12.jsonl")
            .read_text(encoding="utf-8").splitlines()]
    assert _domain_override(rows[10]["problem"], CATEGORIES) == "泛函分析"


@pytest.mark.parametrize(("problem", "expected"), [
    ("Use the closed graph theorem for a derivative operator with the supremum norm.", "泛函分析"),
    ("Use uniform boundedness for bounded linear functionals on a Banach space; study uniform convergence.", "泛函分析"),
    ("A weakly convergent sequence in a Hilbert space is mapped by a compact operator; use uniform convergence.", "泛函分析"),
    ("求实函数的上确界，并证明函数列一致收敛。", "数学分析"),
    ("用 Banach 压缩映射定理证明完备度量空间中迭代的极限。", "数学分析"),
    ("Find the formal adjoint of a differential operator on a Hilbert space for a PDE.", "偏微分方程"),
    ("Study finite element discretization on a Hilbert space and its uniform convergence.", "数值分析"),
    ("Prove the open mapping theorem for holomorphic functions.", None),
    ("Apply the open mapping theorem to Banach spaces with the supremum norm.", "泛函分析"),
])
def test_functional_cues_preserve_neighboring_domains(problem, expected):
    assert _domain_override(problem, CATEGORIES) == expected


def test_missing_functional_domain_is_not_returned():
    problem = "Use the closed graph theorem on a Banach space with the supremum norm."
    assert _domain_override(problem, ["数学分析", "偏微分方程"]) == "数学分析"
