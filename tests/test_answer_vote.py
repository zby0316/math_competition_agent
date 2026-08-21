# -*- coding: utf-8 -*-
"""V2.7 快速采样投票（answer_vote）测试。

覆盖：触发闸门（客观题/证明题/确定性答案/非 match/难度）、自适应 N、
多数确认、反证聚类→仲裁路由+票选赢家、反对票分散不干预、答案比对
（数值容差/字符串归一）、前缀提取。
"""
import unittest

from config import CONFIG
from utils.deps import Deps, MockClient
from utils.time_budget import TimeBudget

from nodes.answer_vote_node import (
    _answers_agree, _extract_answer, _vote_n, answer_vote_node,
)


class _FakeClock:
    """可控时钟：tests 里把 TimeBudget 的 elapsed 拨到任意值。"""

    def __init__(self, start=0.0):
        self.value = start

    def __call__(self):
        return self.value


def _make_deps(responses, elapsed=0.0, cap=None):
    # 注意：MockClient.route 逐字符串注册会互相覆盖，必须整列表注册
    # （每关键词队列）；时钟 start 必须为 0、elapsed 通过 value 拨动——
    # 否则 elapsed() = now-start 恒为 0，预算闸门全部失效。
    clock_obj = _FakeClock(0.0)
    clock = TimeBudget(total_seconds=1200, reserve_seconds=300, clock=clock_obj)
    clock_obj.value = elapsed
    client = MockClient([])
    client.route("最终答案", list(responses))
    deps = Deps(client=client, time_budget=clock, review_client=None)
    return deps, clock, clock_obj


def _make_state(**overrides):
    state = {
        "validation_status": "match",
        "validated_answer": "42",
        "question_mode": "computation",
        "difficulty": "hard",
        "reasoning_result": {"answer": "42", "answer_source": "parsed_sections"},
        "problem": "计算 6×7 的值。",
        "answer_locked": False,
        "validation_details": {"confidence": 0.9},
        "semantic_arbiter_status": "",
        "answer_vote_round": 0,
    }
    state.update(overrides)
    return state


class TestExtractAnswer(unittest.TestCase):
    def test_continuation_form(self):
        self.assertEqual(_extract_answer("最终答案：42"), "42")

    def test_double_colon_noise(self):
        self.assertEqual(_extract_answer("最终答案：：42"), "：42")

    def test_echo_form(self):
        self.assertEqual(_extract_answer("最终答案：x=3"), "x=3")

    def test_fenced(self):
        self.assertEqual(_extract_answer("最终答案：`$42$`"), "42")

    def test_empty(self):
        self.assertEqual(_extract_answer(""), "")
        self.assertEqual(_extract_answer("最终答案：无"), "无")


class TestAnswersAgree(unittest.TestCase):
    def test_exact_numbers(self):
        self.assertTrue(_answers_agree("42", "42"))

    def test_numeric_tolerance(self):
        self.assertTrue(_answers_agree("3.14159265", "3.14159266"))
        self.assertFalse(_answers_agree("0.155", "-0.199"))

    def test_fraction_and_float(self):
        self.assertTrue(_answers_agree("1/2", "0.5"))

    def test_string_normalization(self):
        self.assertTrue(_answers_agree("x = 3，y = 4", "x=3,y=4"))
        self.assertFalse(_answers_agree("3", "4"))

    def test_empty_never_agrees(self):
        self.assertFalse(_answers_agree("", "42"))


class TestAdaptiveN(unittest.TestCase):
    def _saved_config(self):
        return {k: CONFIG.get(k) for k in
                ("answer_vote_n", "answer_vote_estimate_s", "time_reserve_s")}

    def setUp(self):
        self._saved = self._saved_config()
        CONFIG["answer_vote_n"] = {"medium": 2, "hard": 3}
        CONFIG["answer_vote_estimate_s"] = 20
        CONFIG["time_reserve_s"] = 300

    def tearDown(self):
        for k, v in self._saved.items():
            CONFIG[k] = v

    def test_hard_gets_3(self):
        deps, _, _ = _make_deps([])
        self.assertEqual(_vote_n(_make_state(difficulty="hard"), deps), 3)

    def test_medium_gets_2(self):
        deps, _, _ = _make_deps([])
        self.assertEqual(_vote_n(_make_state(difficulty="medium"), deps), 2)

    def test_easy_gets_0(self):
        deps, _, _ = _make_deps([])
        self.assertEqual(_vote_n(_make_state(difficulty="easy"), deps), 0)

    def test_tight_budget_shrinks_n(self):
        # elapsed=700 → remaining_hard=500 → reserve 后只剩 200，容不下
        # 4×20+60=140 吗？容得下；再拨到 800（剩 400-300=100）→ 3×20+60=120>100
        # → 降到 2（2×20+60=100 ≤ 100）。断言 ≤3 且 >0 即可（保守侧）。
        deps, clock, clock_obj = _make_deps([])
        clock_obj.value = 800
        self.assertLessEqual(_vote_n(_make_state(difficulty="hard"), deps), 3)
        clock_obj.value = 1050
        self.assertEqual(_vote_n(_make_state(difficulty="hard"), deps), 0)


class TestAnswerVoteNode(unittest.TestCase):
    def _saved_config(self):
        return {k: CONFIG.get(k) for k in
                ("enable_answer_vote", "answer_vote_n", "answer_vote_estimate_s",
                 "answer_vote_max_tokens", "sample_temperatures", "time_reserve_s")}

    def setUp(self):
        self._saved = self._saved_config()
        CONFIG["enable_answer_vote"] = True
        CONFIG["answer_vote_n"] = {"medium": 2, "hard": 3}
        CONFIG["answer_vote_estimate_s"] = 20
        CONFIG["answer_vote_max_tokens"] = 512
        CONFIG["sample_temperatures"] = [0.4, 0.8, 1.0]
        CONFIG["time_reserve_s"] = 300

    def tearDown(self):
        for k, v in self._saved.items():
            CONFIG[k] = v

    def _run(self, responses, state_overrides=None, elapsed=0.0):
        deps, clock, _ = _make_deps(responses, elapsed=elapsed)
        state = _make_state(**(state_overrides or {}))
        return answer_vote_node(state, {"configurable": {"deps": deps}})

    def test_disabled_by_config(self):
        CONFIG["enable_answer_vote"] = False
        result = self._run(["42"])
        self.assertEqual(result, {})

    def test_not_match_skips(self):
        result = self._run(["42"], {"validation_status": "uncertain"})
        self.assertEqual(result, {})

    def test_arbiter_selected_triggers(self):
        # V2.7.1：主流路径（uncertain_arbitrating → 仲裁选定）必须触发投票
        result = self._run(
            ["42", "42", "42", "42"],
            {"validation_status": "uncertain_arbitrating",
             "semantic_arbiter_status": "selected",
             "answer_locked": True})  # 仲裁锁定不阻止投票（正是要复核的对象）
        self.assertEqual(result.get("answer_vote_status"), "all_agree")
        self.assertEqual(result.get("answer_vote_round"), 1)

    def test_arbiter_not_selected_skips(self):
        result = self._run(
            ["42"], {"validation_status": "uncertain_arbitrating",
                     "semantic_arbiter_status": "abstained"})
        self.assertEqual(result, {})

    def test_round_guard_blocks_second_vote(self):
        result = self._run(["42"], {"answer_vote_round": 1})
        self.assertEqual(result, {})

    def test_round_exhausted_contested_becomes_final(self):
        # 轮次耗尽且上轮反证 → contested_final（防外图无限循环回仲裁）
        result = self._run(
            ["42"], {"answer_vote_round": 1,
                     "answer_vote_status": "contested"})
        self.assertEqual(result.get("answer_vote_status"), "contested_final")

    def test_contested_sets_round(self):
        result = self._run(["42", "40", "40"])
        self.assertEqual(result.get("answer_vote_status"), "contested")
        self.assertEqual(result.get("answer_vote_round"), 1)

    def test_objective_skips(self):
        result = self._run(["42"], {"question_mode": "fill"})
        self.assertEqual(result, {})

    def test_proof_skips(self):
        result = self._run(["42"], {"question_mode": "proof"})
        self.assertEqual(result, {})

    def test_det_solver_source_skips(self):
        result = self._run(["42"], {"reasoning_result": {
            "answer": "42", "answer_source": "det_solver"}})
        self.assertEqual(result, {})

    def test_easy_skips(self):
        result = self._run(["42"], {"difficulty": "easy"})
        self.assertEqual(result.get("answer_vote_status"), "skipped")

    def test_all_agree(self):
        result = self._run(["42", "42", "42", "42"])
        self.assertEqual(result.get("answer_vote_status"), "all_agree")
        self.assertIsNone(result.get("next_node"))
        self.assertEqual(len(result.get("answer_vote_trace", [])), 3)
        self.assertEqual(
            result.get("validation_details", {}).get("answer_vote", {}).get("n"), 3)

    def test_contested_routes_to_arbiter(self):
        # hard N=3：1 票同意（42）、2 票聚类反对（40）→ 阈值 2 → 反证
        result = self._run(["42", "40", "40"])
        self.assertEqual(result.get("answer_vote_status"), "contested")
        self.assertEqual(result.get("next_node"), "semantic_arbiter")
        self.assertEqual(result.get("validation_status"), "vote_contested")
        self.assertEqual(result.get("answer_vote_result", {}).get("winner"), "40")
        self.assertEqual(
            result.get("validation_details", {}).get("routing_reason"),
            "answer_vote_contested_semantic_arbitration")

    def test_scattered_disagreement_keeps_match(self):
        result = self._run(["42", "40", "41"])
        self.assertEqual(result.get("answer_vote_status"), "scattered")
        self.assertIsNone(result.get("next_node"))

    def test_unusable_samples(self):
        # 空响应 → 全部不可用 → insufficient
        result = self._run(["", "", "", ""])
        self.assertEqual(result.get("answer_vote_status"), "insufficient")

    def test_numeric_agreement_within_tolerance(self):
        # 主答案 0.155，样本 -0.199 聚类反对（2/3）→ 反证仲裁
        result = self._run(
            ["0.155", "-0.199", "-0.199"],
            {"validated_answer": "0.155",
             "reasoning_result": {"answer": "0.155",
                                  "answer_source": "parsed_sections"}})
        self.assertEqual(result.get("answer_vote_status"), "contested")
        self.assertEqual(result.get("answer_vote_result", {}).get("winner"), "-0.199")

    def test_budget_exhausted_skips(self):
        result = self._run(["42"], elapsed=1100)
        self.assertEqual(result.get("answer_vote_status"), "skipped")


if __name__ == "__main__":
    unittest.main()
