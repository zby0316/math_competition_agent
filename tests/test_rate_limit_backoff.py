# -*- coding: utf-8 -*-
"""V2 M6 平台防御：限流/配额感知退避测试。

官方评测曾 degraded（eval_log_c0a00cb…）。限流特征（429/配额/-20081）时
不得短退避盲重试——要明显加长退避，保护单题时间预算。
"""
import time
import unittest

from utils.llm_retry import LLMRetryWrapper


class _FakeBudget:
    """无限预算，专注验证退避行为。"""

    def __init__(self):
        self.slept = []

    def now(self):
        return time.monotonic()

    def record(self, *a, **k):
        pass

    def remaining(self):
        return 1e9

    def remaining_hard(self):
        return 1e9

    def expired(self):
        return False

    def can_afford(self, s):
        return True


class _FailClient:
    def __init__(self, error: str, times: int = 2):
        self.error = error
        self.times = times
        self.calls = 0

    def chat(self, *a, **k):
        self.calls += 1
        if self.calls <= self.times:
            raise RuntimeError(self.error)
        return "42"


def _run(error_text: str, times: int = 2):
    """用假时钟采集退避时长。"""
    real_sleep = time.sleep
    sleeps = []
    time.sleep = lambda s: sleeps.append(s)
    try:
        client = _FailClient(error_text, times)
        budget = _FakeBudget()
        wrapper = LLMRetryWrapper(client, max_retries=3, backoff_factor=2.0,
                                  time_budget=budget)
        out = wrapper.chat([{"role": "user", "content": "x"}],
                           temperature=0.0, max_tokens=64)
        return out, sleeps
    finally:
        time.sleep = real_sleep


class TestRateLimitBackoff(unittest.TestCase):

    def test_quota_error_gets_long_backoff(self):
        # 配额错误 → 退避必须 ≥10s（限流特征）
        out, sleeps = _run("Client error 400: 您已超过输出 tokens 配额 code -20081")
        self.assertEqual(out, "42")
        self.assertTrue(all(s >= 10.0 for s in sleeps),
                        f"限流退避应 ≥10s, got {sleeps}")

    def test_429_rate_limit_gets_long_backoff(self):
        out, sleeps = _run("429 Client Error: Too Many Requests")
        self.assertEqual(out, "42")
        self.assertTrue(all(s >= 10.0 for s in sleeps))

    def test_plain_error_gets_standard_backoff(self):
        # 普通错误 → 标准指数退避（1s, 2s），不受限流分支影响
        out, sleeps = _run("connection reset by peer")
        self.assertEqual(out, "42")
        self.assertTrue(all(s < 10.0 for s in sleeps),
                        f"普通错误退避应 <10s, got {sleeps}")

    def test_retry_limit_respected(self):
        # 重试耗尽仍失败 → 抛错
        client = _FailClient("429 too many", times=10)
        wrapper = LLMRetryWrapper(client, max_retries=3, backoff_factor=0,
                                  time_budget=_FakeBudget())
        with self.assertRaises(RuntimeError):
            wrapper.chat([{"role": "user", "content": "x"}],
                         temperature=0.0, max_tokens=64)


if __name__ == "__main__":
    unittest.main()
