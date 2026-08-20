# -*- coding: utf-8 -*-
"""V2.6.2 单题 LLM 请求硬上限测试。

覆盖：默认关闭（0）、计数与耗尽判定、wrapper 超限抛 DeadlineExceeded、
emergency_answer 豁免。
"""
import unittest

from config import CONFIG
from utils.deps import MockClient
from utils.time_budget import TimeBudget


class TestRequestCap(unittest.TestCase):

    def _set_cap(self, cap):
        self._saved = CONFIG.get("max_llm_calls_per_problem")
        CONFIG["max_llm_calls_per_problem"] = cap

    def tearDown(self):
        if hasattr(self, "_saved"):
            CONFIG["max_llm_calls_per_problem"] = self._saved

    def test_cap_zero_means_disabled(self):
        self._set_cap(0)
        tb = TimeBudget(total_seconds=1200, reserve_seconds=300)
        for _ in range(50):
            tb.bump_llm_call()
        self.assertFalse(tb.llm_calls_exhausted())

    def test_cap_counts_and_exhausts(self):
        self._set_cap(3)
        tb = TimeBudget(total_seconds=1200, reserve_seconds=300)
        tb.bump_llm_call()
        tb.bump_llm_call()
        self.assertFalse(tb.llm_calls_exhausted())
        tb.bump_llm_call()
        self.assertTrue(tb.llm_calls_exhausted())
        self.assertEqual(tb.llm_call_count(), 3)

    def test_wrapper_raises_deadline_exceeded_at_cap(self):
        from utils.llm_retry import DeadlineExceeded, LLMRetryWrapper
        self._set_cap(2)
        client = MockClient(["a", "b"])
        tb = TimeBudget(total_seconds=1200, reserve_seconds=300)
        wrapper = LLMRetryWrapper(client, time_budget=tb)
        wrapper.chat([{"role": "user", "content": "hi"}], label="test1")
        wrapper.chat([{"role": "user", "content": "hi"}], label="test2")
        with self.assertRaises(DeadlineExceeded):
            wrapper.chat([{"role": "user", "content": "hi"}], label="test3")

    def test_emergency_answer_exempt(self):
        from utils.llm_retry import LLMRetryWrapper
        self._set_cap(1)
        client = MockClient(["normal", "last_resort"])
        tb = TimeBudget(total_seconds=1200, reserve_seconds=300)
        wrapper = LLMRetryWrapper(client, time_budget=tb)
        # 第 1 次正常调用后计数到达上限
        wrapper.chat([{"role": "user", "content": "hi"}], label="test1")
        self.assertTrue(tb.llm_calls_exhausted())
        # emergency_answer 豁免：保底直答不能被自己挡住
        out = wrapper.chat([{"role": "user", "content": "hi"}], label="emergency_answer")
        self.assertEqual(out, "last_resort")


if __name__ == "__main__":
    unittest.main()
