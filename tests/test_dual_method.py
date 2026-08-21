# -*- coding: utf-8 -*-
"""V2.7-3 数值双方法自洽守卫测试。

覆盖：触发闸门（开关/难度/成功/数值答案/剩余硬预算/API 慢保护/请求上限）、
一致留证、不一致降级 contradict。
"""
import unittest

from config import CONFIG
from utils.deps import Deps, MockClient
from utils.time_budget import TimeBudget

from nodes.python_agent_node import (
    _dual_method_check, _dual_method_eligible,
)


class _FakeClock:
    def __init__(self, v=0.0):
        self.value = v

    def __call__(self):
        return self.value


def _state(difficulty="hard"):
    return {"difficulty": difficulty}


def _enriched(answer="42", success=True):
    return {"success": success, "answer": answer, "evidence_status": "support",
            "evidence_summary": "代码执行成功", "contradictions": []}


def _deps(elapsed=0.0, spend=None, calls=0):
    clock_obj = _FakeClock(0.0)
    clock = TimeBudget(total_seconds=1200, reserve_seconds=300, clock=clock_obj)
    clock_obj.value = elapsed
    for entry in (spend or []):
        clock.record(entry[0], entry[1])
    for _ in range(calls):
        clock.bump_llm_call()
    return Deps(client=MockClient([]), time_budget=clock, review_client=None)


class TestDualMethodEligible(unittest.TestCase):
    def _saved(self):
        return {k: CONFIG.get(k) for k in
                ("enable_dual_method_check", "dual_method_min_remaining_s",
                 "dual_method_max_api_s", "max_llm_calls_per_problem")}

    def setUp(self):
        self._s = self._saved()
        CONFIG["enable_dual_method_check"] = True
        CONFIG["dual_method_min_remaining_s"] = 700
        CONFIG["dual_method_max_api_s"] = 45
        CONFIG["max_llm_calls_per_problem"] = 24

    def tearDown(self):
        for k, v in self._s.items():
            CONFIG[k] = v

    def test_hard_numeric_success_eligible(self):
        self.assertTrue(_dual_method_eligible(
            _state("hard"), _enriched("3.14159"), _deps()))

    def test_config_off(self):
        CONFIG["enable_dual_method_check"] = False
        self.assertFalse(_dual_method_eligible(
            _state("hard"), _enriched(), _deps()))

    def test_not_hard(self):
        self.assertFalse(_dual_method_eligible(
            _state("medium"), _enriched(), _deps()))

    def test_python_failed(self):
        self.assertFalse(_dual_method_eligible(
            _state("hard"), _enriched(success=False), _deps()))

    def test_non_numeric_answer(self):
        self.assertFalse(_dual_method_eligible(
            _state("hard"), _enriched("x^2+1"), _deps()))

    def test_remaining_hard_too_low(self):
        self.assertFalse(_dual_method_eligible(
            _state("hard"), _enriched(), _deps(elapsed=600)))

    def test_slow_api_skips(self):
        self.assertFalse(_dual_method_eligible(
            _state("hard"), _enriched(),
            _deps(spend=[("reasoning", 60), ("python", 50)])))

    def test_fast_api_allows(self):
        self.assertTrue(_dual_method_eligible(
            _state("hard"), _enriched(),
            _deps(spend=[("reasoning", 20), ("python", 25)])))

    def test_request_cap_exhausted_skips(self):
        CONFIG["max_llm_calls_per_problem"] = 3
        self.assertFalse(_dual_method_eligible(
            _state("hard"), _enriched(), _deps(calls=3)))


class TestDualMethodCheck(unittest.TestCase):
    def _run(self, first, second_response):
        from nodes.python_agent_node import _dual_method_check
        deps = _deps()
        client = MockClient([])
        client.route("最终答案", second_response)
        deps.client = client
        enriched = _enriched(first)
        _dual_method_check(deps, _state("hard"), enriched, "题目")
        return enriched

    def test_agree_keeps_evidence(self):
        enriched = self._run("42", "42")
        self.assertEqual(enriched.get("evidence_status"), "support")
        self.assertIn("独立第二方法数值一致", enriched.get("evidence_summary", ""))
        self.assertEqual(enriched.get("dual_method", {}).get("status"), "match")

    def test_mismatch_downgrades_to_contradict(self):
        enriched = self._run("0.155", "-0.199")
        self.assertEqual(enriched.get("evidence_status"), "contradict")
        self.assertTrue(any("独立第二方法" in c
                            for c in enriched.get("contradictions", [])))

    def test_unparseable_second_noop(self):
        enriched = self._run("42", "无法确定")
        self.assertEqual(enriched.get("evidence_status"), "support")
        self.assertNotIn("dual_method", enriched)

    def test_transport_failure_noop(self):
        # client 每次调用都抛异常 → 守卫静默放弃，不改变证据
        class _Boom:
            def chat(self, messages, **kwargs):
                raise RuntimeError("down")
        deps = _deps()
        deps.client = _Boom()
        enriched = _enriched("42")
        _dual_method_check(deps, _state("hard"), enriched, "题目")
        self.assertEqual(enriched.get("evidence_status"), "support")


if __name__ == "__main__":
    unittest.main()
