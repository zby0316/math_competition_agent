"""Round-two behavioral regressions, without network or model API calls."""
import json
from pathlib import Path

import pytest
import sympy as sp

from nodes.classify_domain import _domain_override
from nodes.reason_step import _parse_reasoning_output
from nodes.verify_step import verify_step
from nodes.assemble_step import assemble_step
from utils.answer_compare import AnswerCompare
from utils.answer_schema import coverage_report, missing_components
from utils.deps import Deps, MockClient
from utils.domain_constraints import domain_equivalent
from utils.domain_loader import DomainLoader
from utils.domain_excerpt import select_skill_excerpt
from utils.local_grader import grade_math_answer
from utils.proof_review import extract_review
from utils.verdict_evidence import parse_verification_evidence

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("problem,expected", [
    ("Use Egorov to establish uniform convergence.", "测度积分"),
    ("Measurable functions converge almost everywhere; prove uniform convergence off a small set.", "测度积分"),
    ("可测函数在测度空间上依测度收敛，并讨论一致收敛。", "测度积分"),
    ("随机变量依概率收敛，讨论一致收敛。", "概率论"),
    ("Random variables converge in probability; measurable functions converge almost everywhere.", "概率论"),
    ("A compact operator on a Hilbert space acts on measurable functions converging almost everywhere.", "泛函分析"),
    ("Finite element discretization of a PDE for measurable functions.", "数值分析"),
    ("A PDE has measurable solutions converging almost everywhere.", "偏微分方程"),
    ("Prove uniform convergence of a power series.", "数学分析"),
])
def test_measure_routing_boundaries(problem, expected):
    assert _domain_override(problem, DomainLoader().categories) == expected


def test_actual_egorov_handbook_excerpt():
    rows = [json.loads(line) for line in (ROOT / "benchmarks/proof_gap12.jsonl").read_text(encoding="utf-8").splitlines()]
    loader = DomainLoader()
    category = _domain_override(rows[7]["problem"], loader.categories)
    assert category == "测度积分"
    for size in (3000, 4000):
        excerpt = select_skill_excerpt(loader.get_skill_document(category), rows[7]["problem"], size)
        assert "Egorov" in excerpt
        assert "有限测度" in excerpt


def test_conditional_count_does_not_force_feasibility():
    problem = "Is it possible to cover the set? If it is possible, what is the minimum number needed?"
    report = coverage_report(problem, "Impossible: a necessary parity condition is contradicted.")
    assert "feasibility_count" not in report["missing"]
    assert "conditional_count_requires_argument_check" in report["unknown"]
    assert report["mathematical_correctness_verified"] is False
    assert "feasibility_count" in missing_components(problem, "Yes, it is possible.")


def test_named_quantities_require_values_and_keep_correct_candidate():
    problem = "求 $a$ 和 $b$。"
    assert missing_components(problem, "a=2") == ["field:b"]
    assert missing_components(problem, "a=2; b=?") == ["field:b"]
    assert missing_components(problem, "a=2; b=3") == []
    from nodes.arbiter_step import _Candidate, _enforce_contract
    partial = _Candidate("python", "a=2", "")
    complete = _Candidate("reasoning", "a=2; b=3", "")
    assert _enforce_contract(problem, partial, [partial, complete])[0] is complete
    assert _enforce_contract(problem, partial, [partial, complete], unresolved_contradiction=True)[0] is partial
    wrong_complete = _Candidate("reasoning", "a=9; b=3", "")
    assert _enforce_contract(problem, partial, [partial, wrong_complete])[0] is partial


def test_unknown_proof_and_parameter_coverage_is_not_certified():
    report = coverage_report("证明并给反例：(1)讨论参数的适用条件；(2)给反例。", "成立")
    assert len(report["unknown"]) == 3


def test_nonnumbered_proof_body_survives_parser_and_coordinator():
    body = "取任意闭集 F。它是紧致的，连续像也是紧致的。由目标空间 Hausdorff，像为闭集，因此逆映射连续。"
    raw = "## 问题分析\n闭映射证明。\n## 详细解题步骤\n" + body + "\n## 最终答案\n映射是同胚。"
    parsed = _parse_reasoning_output(raw, "proof")
    assert parsed["proof_body"] == body
    assert parsed["steps"][0]["description"] == body
    client = MockClient([body + '\n<!-- PROOF_REVIEW {"status":"none_reported","issues":[]} -->'])
    result = assemble_step({"problem": "证明映射是同胚", "category": "拓扑学",
                            "reasoning_result": parsed, "reasoning_raw_response": raw,
                            "validation_details": {"problem_type": "proof"}},
                           {"configurable": {"deps": Deps(client=client)}})
    assert len(client.calls) == 1
    assert body in client.calls[0]["messages"][0]["content"]
    assert "PROOF_REVIEW" not in result["final_response"]
    assert result["proof_review"]["mathematical_correctness_verified"] is False


@pytest.mark.parametrize("raw", ["证明正文", '<!-- PROOF_REVIEW {"status":', '<!-- PROOF_REVIEW {} -->'])
def test_bad_review_note_remains_unknown(raw):
    visible, review = extract_review(raw)
    assert review["status"] == "unknown"
    assert "PROOF_REVIEW" not in visible


class ExecutorSpy:
    def __init__(self):
        self.codes = []
    def execute(self, code, timeout):
        self.codes.append(code)
        return {"success": True, "stdout": "验证状态: INCONCLUSIVE\n最终答案: 2\n", "answer": "2"}


def test_syntax_error_feedback_precedes_execution_and_retry_is_bounded():
    executor = ExecutorSpy()
    client = MockClient(['```python\nx = (\n```', '```python\nanswer = "2"\nprint("最终答案:", answer)\n```'])
    result = verify_step({"problem": "计算 1+1", "category": "数学分析", "question_mode": "computation"},
                         {"configurable": {"deps": Deps(client=client, skills_loader=DomainLoader(), mcp_client=executor)}})
    assert len(executor.codes) == 1
    assert "语法检查失败" in client.calls[1]["messages"][0]["content"]
    assert result["python_trace"][0]["failure_kind"] == "syntax_failed"
    assert result["python_attempts"] == 2
    bad = MockClient(['```python\nx = (\n```'] * 2)
    result = verify_step({"problem": "计算 1+1", "category": "数学分析"},
                         {"configurable": {"deps": Deps(client=bad, skills_loader=DomainLoader(), mcp_client=executor)}})
    assert result["python_attempts"] == 2
    assert len(executor.codes) == 1


@pytest.mark.parametrize("left,right,expected", [
    ("sqrt(t+1)*sqrt(1/(t+1))", "1", True),
    ("sqrt(t**2)", "t", True),
    ("sqrt(t**2)", "-t", None),
    ("nan", "1", None),
])
def test_domain_aware_symbolic_verification(left, right, expected):
    assert domain_equivalent("Solve the PDE for $t>0$.", left, right) is expected


def test_sampling_agreement_is_not_symbolic_proof():
    x = sp.Symbol("x")
    assert AnswerCompare._numeric_sample_equivalent(sp.sin(x), sp.sin(x), 1e-6) is None
    assert AnswerCompare._numeric_sample_equivalent(x, x + 1, 1e-6) is False
    assert domain_equivalent("Find the answer", "sqrt(t**2)", "t") is None


@pytest.mark.parametrize("evidence", ["无法化简，不能判定", "could not simplify difference", "counterexample outside domain x=-1"])
def test_uncertain_or_out_of_domain_fail_is_not_counterexample(evidence):
    output = parse_verification_evidence({"success": True, "stdout": "验证状态: FAIL\n验证证据: " + evidence})
    assert output["evidence_status"] == "inconclusive"


@pytest.mark.parametrize("problem,predicted,reference,expected", [
    ("Find the set", "{3,1,2}", "{1,2,3}", True),
    ("Find the set", "{1,2}", "{1,2,3}", False),
    ("多选题：选择正确选项 A.甲 B.乙 C.丙", "C,A", "A,C", True),
    ("Find a and b", "a=2; b=3", "a=2; b=4", False),
    ("Find a and b", "a=2", "a=2; b=4", None),
    ("Simplify for $t>0$", "sqrt(t+1)*sqrt(1/(t+1))", "1", True),
    ("Prove convergence", "same conclusion", "same conclusion", None),
])
def test_conservative_grading(problem, predicted, reference, expected):
    assert grade_math_answer(problem, predicted, reference)[0] is expected
