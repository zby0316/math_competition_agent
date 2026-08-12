# -*- coding: utf-8 -*-
"""V2 M2 置信门控测试：资源档位分配正确性。

覆盖：高置信客观题跳过 Python / 低置信触发深通道 / 实算填空不跳验证 /
中置信维持双路。
"""
import unittest

from utils.confidence_gate import (can_skip_python_verify, confidence_level,
                                   wants_deep_solver)


class TestConfidenceGate(unittest.TestCase):

    def test_level_mapping(self):
        self.assertEqual(confidence_level(0.95), "fast")
        self.assertEqual(confidence_level(0.90), "fast")
        self.assertEqual(confidence_level(0.80), "standard")
        self.assertEqual(confidence_level(0.70), "standard")
        self.assertEqual(confidence_level(0.60), "deep")
        self.assertEqual(confidence_level(0.10), "deep")
        self.assertEqual(confidence_level(None), "standard")

    def test_high_confidence_concept_fill_skips_python(self):
        # 高置信概念填空 → 跳过 Python（省资源）
        self.assertTrue(can_skip_python_verify(
            "fill", 0.95, "请问二阶矩是指什么？"))

    def test_high_confidence_choice_skips_python(self):
        self.assertTrue(can_skip_python_verify("choice", 0.92))

    def test_low_confidence_never_skips_python(self):
        # 低置信 → 必须验证
        self.assertFalse(can_skip_python_verify("fill", 0.55, "请问二阶矩是指什么"))
        self.assertFalse(can_skip_python_verify("choice", 0.60))

    def test_computation_fill_keeps_python(self):
        # 实算填空（含数字/计算信号）→ 保留 Python 验证（治本机制）
        self.assertFalse(can_skip_python_verify(
            "fill", 0.95, "求导数 f'(x)，其中 f(x)=x^2+3x"))

    def test_computation_mode_never_skips(self):
        self.assertFalse(can_skip_python_verify("computation", 0.99))

    def test_deep_solver_trigger(self):
        # 低置信计算题 → 深通道
        self.assertTrue(wants_deep_solver("computation", 0.55))
        # 高置信计算题 → 不深通道（省资源）
        self.assertFalse(wants_deep_solver("computation", 0.95))
        # 客观题永不深通道（快速路径最优）
        self.assertFalse(wants_deep_solver("choice", 0.50))
        self.assertFalse(wants_deep_solver("true_false", 0.50))
        # 深思考领域（数论）→ 触发
        self.assertTrue(wants_deep_solver("computation", 0.85, category="数论"))
        # 显式 hard 难度 → 触发
        self.assertTrue(wants_deep_solver("proof", 0.85, difficulty="hard"))


if __name__ == "__main__":
    unittest.main()
