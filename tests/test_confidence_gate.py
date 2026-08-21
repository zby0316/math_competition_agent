# -*- coding: utf-8 -*-
"""V2 M2 置信门控测试：资源档位分配正确性。

覆盖：档位映射 / 深通道触发条件（客观题不深、低置信深、深思考领域深）。
V2.7.2：can_skip_python_verify 死代码已移除（运行时代码全部走
verify_router.needs_python_verify），对应测试同步移除。
"""
import unittest

from utils.confidence_gate import confidence_level, wants_deep_solver


class TestConfidenceGate(unittest.TestCase):

    def test_level_mapping(self):
        self.assertEqual(confidence_level(0.95), "fast")
        self.assertEqual(confidence_level(0.90), "fast")
        self.assertEqual(confidence_level(0.80), "standard")
        self.assertEqual(confidence_level(0.70), "standard")
        self.assertEqual(confidence_level(0.60), "deep")
        self.assertEqual(confidence_level(0.10), "deep")
        self.assertEqual(confidence_level(None), "standard")

    def test_deep_solver_trigger(self):
        # 低置信计算题 → 深通道
        self.assertTrue(wants_deep_solver("computation", 0.55))
        # 高置信计算题 → 不深通道（省资源）
        self.assertFalse(wants_deep_solver("computation", 0.95))
        # 客观题永不深通道（快速路径最优）
        self.assertFalse(wants_deep_solver("choice", 0.50))
        self.assertFalse(wants_deep_solver("true_false", 0.50))
        # 深思考领域（离散数学，V2.6.1 对齐官方 18 领域名）→ 触发
        self.assertTrue(wants_deep_solver("computation", 0.85, category="离散数学"))
        # 显式 hard 难度 → 触发
        self.assertTrue(wants_deep_solver("proof", 0.85, difficulty="hard"))


if __name__ == "__main__":
    unittest.main()
