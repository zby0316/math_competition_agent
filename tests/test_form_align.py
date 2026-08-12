# -*- coding: utf-8 -*-
"""V2 M4 答案形式对齐测试。

覆盖：单值/区间/判断/枚举形态识别 + idx=94 真实失分场景复现。
"""
import unittest

from utils.form_align import check_form_alignment, expected_form


class TestExpectedForm(unittest.TestCase):

    def test_single_value_signals(self):
        self.assertEqual(expected_form("求 x 的值"), "single")
        self.assertEqual(expected_form("计算定积分的结果"), "single")
        self.assertEqual(expected_form("求半长"), "single")
        self.assertEqual(expected_form("问该集合元素个数"), "single")
        self.assertEqual(expected_form("求 n 的最大值"), "single")

    def test_interval_signals(self):
        self.assertEqual(expected_form("求 x 的取值范围"), "interval")
        self.assertEqual(expected_form("求置信区间"), "interval")
        self.assertEqual(expected_form("求定义域"), "interval")

    def test_judge_signals(self):
        self.assertEqual(expected_form("判断是否成立"), "judge")
        self.assertEqual(expected_form("该结论对吗"), "judge")
        self.assertEqual(expected_form("是否存在这样的 n"), "judge")

    def test_enum_signals(self):
        self.assertEqual(expected_form("列出所有解"), "enum")
        self.assertEqual(expected_form("求 x 的所有取值"), "enum")

    def test_no_signal(self):
        self.assertEqual(expected_form("解方程"), "unknown")


class TestCheckFormAlignment(unittest.TestCase):

    def test_idx94_confidence_interval_scenario(self):
        """真实失分复现：问半长（单值），答区间 → 错配。

        标准答案 0.49，智能体答 [2.01, 2.99]（数学对但形式不合）。
        """
        problem = ("设 X~N(mu,1)，n=16，求 mu 的 95% 置信区间长度的一半"
                   "（即 z_{0.025}*1/sqrt(n) 的数值）")
        # 智能体答了完整区间
        r = check_form_alignment(problem, "置信区间：[2.01, 2.99]")
        self.assertFalse(r["aligned"])
        self.assertEqual(r["expected"], "single")
        # 答了单值 → 对齐
        r2 = check_form_alignment(problem, "0.49")
        self.assertTrue(r2["aligned"])

    def test_single_answer_matches(self):
        r = check_form_alignment("求 x 的值", "x = 5")
        self.assertTrue(r["aligned"])

    def test_interval_answer_matches(self):
        r = check_form_alignment("求 x 的取值范围", "[1, 3]")
        self.assertTrue(r["aligned"])
        r2 = check_form_alignment("求定义域", "(-inf, 0) ∪ (0, +inf)")
        self.assertTrue(r2["aligned"])

    def test_judge_answer_matches(self):
        r = check_form_alignment("判断该命题是否成立", "是")
        self.assertTrue(r["aligned"])
        r2 = check_form_alignment("判断该命题是否成立", "结论：成立")
        self.assertTrue(r2["aligned"])
        # 判断题答了数值 → 错配
        r3 = check_form_alignment("判断该命题是否成立", "5")
        self.assertFalse(r3["aligned"])

    def test_unknown_problem_passes(self):
        r = check_form_alignment("解方程", "x = 2")
        self.assertTrue(r["aligned"])


if __name__ == "__main__":
    unittest.main()
