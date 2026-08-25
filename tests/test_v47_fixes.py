# -*- coding: utf-8 -*-
"""V4.7：通用 Python 双验证触发 + SC 散文接管。"""
import sys

import pytest

sys.path.insert(0, ".")  # noqa: E402

from nodes.cross_validator_node import _python_support_wants_verify  # noqa: E402


def _state(rz, pa, ev="support"):
    return {
        "problem": "x", "question_mode": "computation",
        "second_python_done": False,
        "reasoning_result": {"answer": rz},
        "python_output": {"answer": pa, "evidence_status": ev,
                          "authenticity": {"fabricated": False}, "skipped": False},
    }


class TestPythonSupportWantsVerify:
    def test_support_closed_python_prose_reasoning(self):
        """#10 类：python 闭式 + 推理散文 → 触发双验证。"""
        s = _state("验证无更小值：通过拉格朗日乘子法…", "-1/8")
        assert _python_support_wants_verify(
            s, None, s["python_output"], s["reasoning_result"]) is True

    def test_support_both_closed_conflict(self):
        """#106 类：两闭式不同（真冲突）→ 触发双验证。"""
        s = _state("9", "5")
        assert _python_support_wants_verify(
            s, None, s["python_output"], s["reasoning_result"]) is True

    def test_evidence_not_support(self):
        """#35/#108 类：evidence=contradict → 不触发（锁 python 有风险）。"""
        s = _state("散文", "1/2", ev="contradict")
        assert _python_support_wants_verify(
            s, None, s["python_output"], s["reasoning_result"]) is False

    def test_python_not_closed(self):
        s = _state("9", "无法确定：题目缺少条件")
        assert _python_support_wants_verify(
            s, None, s["python_output"], s["reasoning_result"]) is False

    def test_second_done_skipped(self):
        s = _state("9", "5")
        s["second_python_done"] = True
        assert _python_support_wants_verify(
            s, None, s["python_output"], s["reasoning_result"]) is False

    def test_both_closed_equivalent(self):
        """两闭式等价 → 不触发（已是 match 场景）。"""
        s = _state("0.5", "1/2")
        assert _python_support_wants_verify(
            s, None, s["python_output"], s["reasoning_result"]) is False

    def test_fabricated_skipped(self):
        s = _state("9", "5")
        s["python_output"]["authenticity"] = {"fabricated": True}
        assert _python_support_wants_verify(
            s, None, s["python_output"], s["reasoning_result"]) is False


class TestSCProseTakeover:
    def test_prose_main_takeover_closed_sample(self):
        """#10 类：主链路散文 + SC 唯一闭式采样 → 锁定闭式。"""
        from nodes.sc_consensus_node import same_answer
        # 构造 sc_consensus_node 的散文接管场景（直接验证 same_answer 判据）
        assert same_answer("-1/8", r"-\frac{1}{8}") is True
        assert same_answer("验证无更小值：通过拉格朗日乘子法求出所有临界点",
                           "-1/8") is False

    def test_closed_main_no_takeover(self):
        """主链路闭式 → 散文接管不适用（唯一性检查外的正常 2/3 路径）。"""
        from nodes.sc_consensus_node import same_answer
        assert same_answer("44", "44") is True
        assert same_answer("44", "100") is False
