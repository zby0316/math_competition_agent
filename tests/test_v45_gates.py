# -*- coding: utf-8 -*-
"""V4.5：跑题拦截 + 假性放弃重读 + 全放弃候选跳过仲裁 单元测试。"""
import sys

import pytest

sys.path.insert(0, ".")  # noqa: E402

from utils.off_topic_guard import answer_off_topic  # noqa: E402


class TestOffTopicGuard:
    def test_digits_off_topic(self):
        """#16 类：65 位大整数与题面数字无公共子串 → 拦截。"""
        problem = "Find the value of (2017+1)/3 for n=2017."
        answer = "最终答案：7524389324549354450012295667238056650488661292408472865"
        assert answer_off_topic(problem, answer) is True

    def test_digits_on_topic(self):
        """答案数字含题面数字子串 → 不拦截。"""
        problem = "Find the value of (2017+1)/3."
        answer = "最终答案：2017 的倍数 6048"
        assert answer_off_topic(problem, answer) is False

    def test_short_answer_not_flagged(self):
        """短答案（<8 字符）无法可靠判定 → 不拦截。"""
        assert answer_off_topic("Compute 2+2.", "最终答案：4") is False
        assert answer_off_topic("Compute 2+2.", "4") is False

    def test_words_off_topic(self):
        """英文实体词零交集 → 拦截。"""
        problem = "Find the number of lattice points in a circle of radius 2009."
        answer = "最终答案：The coin game has 7 winning strategies for Mario."
        assert answer_off_topic(problem, answer) is True

    def test_words_on_topic(self):
        problem = "Find the number of lattice points in a circle of radius 2009."
        answer = "最终答案：The number of lattice points is 12679645, computed by enumeration."
        assert answer_off_topic(problem, answer) is False

    def test_empty_inputs(self):
        assert answer_off_topic("", "") is False
        assert answer_off_topic("problem", "") is False


class TestGiveUpRereadTrigger:
    def test_looks_like_give_up(self):
        from utils.hard_fallback import looks_like_give_up
        assert looks_like_give_up("题面缺少 m 与 n 的具体数值或约束条件，无法确定") is True
        assert looks_like_give_up("无法完成：题目条件不足") is True
        assert looks_like_give_up("最终答案：3") is False
        assert looks_like_give_up("") is True  # 空串视为放弃

    def test_reread_loop_flag_in_module(self):
        """reasoning_agent_node 中 _reread_used 只在循环内出现一次声明。"""
        import importlib
        m = importlib.import_module("nodes.reasoning_agent_node")
        src = open(m.__file__, encoding="utf-8").read()
        assert src.count("_reread_used = False") == 1
        assert "reread_give_up" in src
        assert "looks_like_give_up" in src


class TestSkipArbitrationLogic:
    def _state(self, rz, pa):
        return {
            "question_mode": "computation",
            "reasoning_result": {"answer": rz},
            "python_output": {"answer": pa, "evidence_status": "inconclusive",
                              "authenticity": {"fabricated": False}, "skipped": False},
        }

    def test_give_up_candidates_detected(self):
        """两候选都放弃型且非闭式 → 应触发跳过仲裁（模拟 reconcile 分支条件）。"""
        from utils.hard_fallback import looks_like_give_up
        from utils.rule_judge import is_closed_form_answer
        ra = "题面缺少条件，无法确定 n-m 的值"
        pa = "无法确定：题目信息不足"
        assert looks_like_give_up(ra) and looks_like_give_up(pa)
        assert not is_closed_form_answer(ra) and not is_closed_form_answer(pa)

    def test_closed_candidate_not_skipped(self):
        from utils.rule_judge import is_closed_form_answer
        assert is_closed_form_answer("130348") is True
        assert is_closed_form_answer("无法确定：题目信息不足") is False
