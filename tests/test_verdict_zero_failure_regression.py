"""回归：声明"零个失败"的行不得被判成反例。

背景
----
``_CONTRADICTION_RE`` 里的 ``FAIL`` 没有词边界，是子串匹配。于是 stdout 里一行
``fail_count: 0`` 会被判成反例，整题得到 ``evidence_status="contradict"``；下游
``_evidence_override`` 随即用 Python 分支的答案顶掉推理分支的正确答案，产出
"答案与自带步骤互相矛盾"的交付文本（实测：推理得 42、最终写 99，步骤里仍写 42），
并白白多跑一轮 reconciliation。

修复只加零值防护、不收窄 ``_CONTRADICTION_RE``，因此 ``fail_count: 3`` 这类
非零计数仍然是货真价实的反例信号。
"""

from utils.verdict_evidence import parse_verification_evidence


def _run(line: str, marker: str = "PASS"):
    return parse_verification_evidence(
        {"stdout": f"验证状态: {marker}\n{line}\n", "success": True, "answer": "99"},
        candidate_answer="42",
    )


def test_zero_failure_count_is_not_a_contradiction():
    result = _run("fail_count: 0")
    assert result["evidence_status"] != "contradict"
    assert result["contradictions"] == []


def test_zero_mismatches_is_not_a_contradiction():
    result = _run("0 mismatches")
    assert result["evidence_status"] != "contradict"


def test_chinese_zero_counterexample_is_not_a_contradiction():
    result = _run("反例: 0")
    assert result["evidence_status"] != "contradict"


def test_nonzero_failure_count_is_still_a_contradiction():
    result = _run("fail_count: 3")
    assert result["evidence_status"] == "contradict"
    assert result["contradictions"]


def test_textual_counterexample_is_still_a_contradiction():
    result = _run("反例: x=2 时等式不成立")
    assert result["evidence_status"] == "contradict"
