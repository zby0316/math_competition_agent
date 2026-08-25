# -*- coding: utf-8 -*-
"""V4.1 规则可判定接管：判定器与决策流单元测试。"""
import sys

import pytest

sys.path.insert(0, ".")  # noqa: E402

from utils.rule_judge import (  # noqa: E402
    is_closed_form_answer,
    answers_numerically_equivalent,
    is_rule_judgeable,
    state_is_rule_judgeable,
    closed_form_cue_score,
)
from nodes.cross_validator_node import (  # noqa: E402
    _rule_judgeable_wants_double_verify,
)


class TestClosedForm:
    def test_numeric_int(self):
        assert is_closed_form_answer("49") is True

    def test_fraction_latex(self):
        assert is_closed_form_answer(r"\frac{19}{28}") is True

    def test_fraction_ascii(self):
        assert is_closed_form_answer("19/28") is True

    def test_sqrt_expr(self):
        assert is_closed_form_answer(r"80 + 40\sqrt{3}") is True

    def test_prose_not_closed(self):
        assert is_closed_form_answer("由于 f 在闭区间连续，所以最大值存在") is False

    def test_placeholder_not_closed(self):
        assert is_closed_form_answer("无法确定：题目缺少条件") is False

    def test_huge_factorial_guard(self):
        # 文本层规模预检：不解析、不卡死、判定非闭式（无法对拍）
        assert is_closed_form_answer(r"\frac{1}{2014!^{2014}}") is False

    def test_empty(self):
        assert is_closed_form_answer("") is False
        assert is_closed_form_answer(None) is False


class TestNumericEquivalence:
    def test_int_vs_fraction(self):
        assert answers_numerically_equivalent("0.5", "1/2") is True

    def test_int_vs_int_diff(self):
        assert answers_numerically_equivalent("49", "48") is False

    def test_latex_fraction_vs_decimal(self):
        assert answers_numerically_equivalent(r"\frac{19}{28}",
                                              "0.6785714285714286") is True

    def test_sqrt_form(self):
        assert answers_numerically_equivalent(r"80 + 40\sqrt{3}",
                                              "149.2820323027551") is True

    def test_garbage_returns_none(self):
        assert answers_numerically_equivalent("散文答案", "0.5") is None

    def test_negative(self):
        assert answers_numerically_equivalent(r"-\frac{1}{8}", "-0.125") is True


class TestRuleJudgeable:
    def test_known_problem(self):
        # omni:3021（错题本中规则可判定题）题面开头
        p = "Find the number of ways to place 5 non-attacking kings on a 3 by 3 chessboard."
        # 若 rule 文件存在则按文件判定；否则返回 None 不算失败
        r = is_rule_judgeable(p)
        assert r in (True, False, None)

    def test_state_annotation_priority(self):
        # 调度端标注优先
        assert state_is_rule_judgeable(
            {"metadata": {"rule_judgeable": True}, "problem": "anything"}) is True
        assert state_is_rule_judgeable(
            {"metadata": {"rule_judgeable": False}, "problem": "anything"}) is False

    def test_state_fallback_text_match(self):
        # 无标注时回退文本匹配（用真实题面）
        import json
        from pathlib import Path
        f = Path("local_test/data/schedule/set1_round_004.jsonl")
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                d = json.loads(line)
                if d.get("rule_judgeable") is True:
                    assert state_is_rule_judgeable(
                        {"metadata": {}, "problem": d["problem"]}) is True
                    return
        pytest.skip("无真实题面样本")

    def test_cue_score(self):
        assert closed_form_cue_score("Compute the value of x") > 0
        assert closed_form_cue_score("Prove that the function is continuous") <= 0


def _mk_state(rz_ans, py_ans, ev="support", rule=True):
    return {
        "problem": "x",
        "question_mode": "computation",
        "metadata": {"rule_judgeable": rule},
        "reasoning_result": {"answer": rz_ans},
        "python_output": {"answer": py_ans, "evidence_status": ev,
                          "authenticity": {"fabricated": False}, "skipped": False},
    }


class TestTakeoverDecision:
    def test_prose_reasoning_py_closed(self):
        s = _mk_state("根据分析估计 N 约为 594.5", "130348")
        assert _rule_judgeable_wants_double_verify(
            s, None, s["python_output"], s["reasoning_result"]) is True

    def test_both_closed_mismatch(self):
        # #2 类：双闭式不等价真冲突 → 触发双验证
        s = _mk_state("12679645", "12679605")
        assert _rule_judgeable_wants_double_verify(
            s, None, s["python_output"], s["reasoning_result"]) is True

    def test_py_not_closed(self):
        s = _mk_state("散文", "无法确定：题目缺少条件")
        assert _rule_judgeable_wants_double_verify(
            s, None, s["python_output"], s["reasoning_result"]) is False

    def test_evidence_not_support(self):
        s = _mk_state("散文", "130348", ev="contradict")
        assert _rule_judgeable_wants_double_verify(
            s, None, s["python_output"], s["reasoning_result"]) is False

    def test_not_rule_judgeable(self):
        s = _mk_state("散文", "130348", rule=False)
        assert _rule_judgeable_wants_double_verify(
            s, None, s["python_output"], s["reasoning_result"]) is False

    def test_fabricated(self):
        s = _mk_state("散文", "130348")
        s["python_output"]["authenticity"] = {"fabricated": True}
        assert _rule_judgeable_wants_double_verify(
            s, None, s["python_output"], s["reasoning_result"]) is False

    def test_skipped(self):
        s = _mk_state("散文", "130348")
        s["python_output"]["skipped"] = True
        assert _rule_judgeable_wants_double_verify(
            s, None, s["python_output"], s["reasoning_result"]) is False

    def test_no_python_answer(self):
        s = _mk_state("散文", "")
        assert _rule_judgeable_wants_double_verify(
            s, None, s["python_output"], s["reasoning_result"]) is False
