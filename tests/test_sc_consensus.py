# -*- coding: utf-8 -*-
"""V4.6 SC 共识节点：触发判定 / 文本等价 / 投票锁定逻辑。"""
import sys

import pytest

sys.path.insert(0, ".")  # noqa: E402

from nodes.sc_consensus_node import (  # noqa: E402
    _wants_sc,
    same_answer,
    _text_equiv,
)


def _state(**kw):
    s = {
        "problem": "x", "question_mode": "computation",
        "difficulty": "hard", "validation_status": "uncertain_arbitrating",
        "metadata": {"rule_judgeable": True},
    }
    s.update(kw)
    return s


class TestTextEquiv:
    def test_same_after_normalize(self):
        assert _text_equiv("Player B has a winning strategy.", "Player B has a winning strategy") is True
        assert _text_equiv("答案是 5。", "答案是 5") is True

    def test_different(self):
        assert _text_equiv("Player A wins", "Player B wins") is False

    def test_too_short(self):
        assert _text_equiv("A", "A") is False  # len<3


class TestSameAnswer:
    def test_numeric(self):
        assert same_answer("0.5", "1/2") is True
        assert same_answer("49", "48") is False

    def test_latex_fraction(self):
        assert same_answer(r"\frac{2}{3}", "2/3") is True

    def test_text(self):
        assert same_answer("The sum diverges.", "级数发散") is False  # 中英不匹配
        assert same_answer("B has a winning strategy", "B has a winning strategy.") is True


class TestWantsSC:
    def test_trigger_hard_uncertain(self):
        assert _wants_sc(_state(), {"configurable": {"deps": None}}) is True

    def test_match_skipped(self):
        assert _wants_sc(_state(validation_status="match"),
                         {"configurable": {"deps": None}}) is False

    def test_proof_skipped(self):
        assert _wants_sc(_state(question_mode="proof"),
                         {"configurable": {"deps": None}}) is False

    def test_easy_non_rule_skipped(self):
        assert _wants_sc(_state(difficulty="easy",
                                metadata={"rule_judgeable": False}),
                         {"configurable": {"deps": None}}) is False

    def test_config_off(self):
        import config as _cfg
        old = _cfg.CONFIG.get("enable_sc_consensus", True)
        _cfg.CONFIG["enable_sc_consensus"] = False
        try:
            assert _wants_sc(_state(), {"configurable": {"deps": None}}) is False
        finally:
            _cfg.CONFIG["enable_sc_consensus"] = old
