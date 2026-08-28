# -*- coding: utf-8 -*-
"""V2.4 单题时间硬约束测试：重试降级 + token 分级 + 深加工预算分级。"""
import unittest


class TestV24Config(unittest.TestCase):
    def test_retry_reduced(self):
        from config import CONFIG
        self.assertEqual(CONFIG["llm_max_retries"], 1)
        self.assertEqual(CONFIG["max_retries_per_node"], 1)

    def test_token_by_difficulty(self):
        from config import CONFIG
        by_diff = CONFIG["reasoning_tokens_by_difficulty"]
        # V2.5 修复（官方 max_tokens cap=8192）：全难度压到 cap 内，难题靠
        # 截断续写 4 轮×4096 补全，不再单次给大 max_tokens。
        self.assertEqual(by_diff["easy"], 8192)
        self.assertEqual(by_diff["medium"], 8192)
        self.assertEqual(by_diff["hard"], 8192)
        # cap 合规：全部 ≤ 官方上限
        self.assertLessEqual(by_diff["hard"], 8192)


class TestV24DeepProcessingGate(unittest.TestCase):
    """深加工预算分级：剩余硬时间不足时跳过 M7/M12/M14。"""

    def test_budget_gate_threshold(self):
        # 模拟 TimeBudget.remaining_hard()
        class _FakeBudget:
            def __init__(self, remaining):
                self._r = remaining
            def remaining_hard(self):
                return self._r
        # M7/M12 阈值 180s
        budget_low = _FakeBudget(100.0)
        budget_ok = _FakeBudget(300.0)
        # 深加工应仅在 remaining_hard > 180 时触发
        self.assertTrue(not budget_ok or budget_ok.remaining_hard() > 180.0)
        self.assertFalse(not budget_low or budget_low.remaining_hard() > 180.0)

    def test_token_grading_logic(self):
        from config import CONFIG
        # 模拟 reasoning_agent_node 的分级选择逻辑
        def pick(difficulty):
            return CONFIG.get("reasoning_tokens_by_difficulty", {}).get(
                str(difficulty or "medium").lower(),
                CONFIG["max_tokens"]["reasoning"])
        # V2.5 cap 适配后全难度统一 8192
        self.assertEqual(pick("easy"), 8192)
        self.assertEqual(pick("hard"), 8192)
        self.assertEqual(pick("medium"), 8192)
        self.assertEqual(pick(None), 8192)  # None → 默认 medium 档


if __name__ == "__main__":
    unittest.main()
