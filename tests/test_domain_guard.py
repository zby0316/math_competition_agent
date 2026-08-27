# -*- coding: utf-8 -*-
"""V2.5 零风险防御测试：答案定义域守卫 + 运算符截断信号。"""
import unittest

from utils.answer_domain_guard import check_answer_domain
from utils.llm_retry import _looks_truncated


class TestProbabilityRange(unittest.TestCase):
    def test_probability_over_1(self):
        r = check_answer_domain("What is the probability that the arrow stops?", "3/2")
        self.assertTrue(r["violation"])
        self.assertEqual(r["kind"], "probability_range")

    def test_probability_negative(self):
        r = check_answer_domain("求事件发生的概率。", "-0.5")
        self.assertTrue(r["violation"])

    def test_probability_valid(self):
        r = check_answer_domain("求事件发生的概率。", "2/3")
        self.assertFalse(r["violation"])

    def test_probability_one(self):
        r = check_answer_domain("求事件发生的概率。", "1")
        self.assertFalse(r["violation"])


class TestPercentRange(unittest.TestCase):
    def test_percent_over_100(self):
        r = check_answer_domain("What percent of students chose pizza?", "150%")
        self.assertTrue(r["violation"])
        self.assertEqual(r["kind"], "percent_range")

    def test_percent_valid(self):
        r = check_answer_domain("What percent of students chose pizza?", "75%")
        self.assertFalse(r["violation"])


class TestAngleRange(unittest.TestCase):
    def test_angle_over_360(self):
        r = check_answer_domain("求角度（degrees）。", "450")
        self.assertTrue(r["violation"])

    def test_angle_valid(self):
        r = check_answer_domain("求角度（degrees）。", "54")
        self.assertFalse(r["violation"])

    def test_angle_modulo_context_not_blocked(self):
        # 同余语境：角度可取模，越界可能合法
        r = check_answer_domain("求 x mod 360 的同余角度。", "720")
        self.assertFalse(r["violation"])


class TestNegativeCount(unittest.TestCase):
    def test_negative_count(self):
        r = check_answer_domain("How many ways are there?", "-3")
        self.assertTrue(r["violation"])

    def test_valid_count(self):
        r = check_answer_domain("How many ways are there?", "16")
        self.assertFalse(r["violation"])


class TestNoFalsePositive(unittest.TestCase):
    def test_non_numeric_answer(self):
        r = check_answer_domain("求概率。", "无法确定")
        self.assertFalse(r["violation"])

    def test_expression_answer(self):
        r = check_answer_domain("求概率。", "1 - p")
        self.assertFalse(r["violation"])

    def test_unrelated_problem(self):
        r = check_answer_domain("求 x 的值，已知 2x+3=7。", "2")
        self.assertFalse(r["violation"])

    def test_latex_wrapped(self):
        r = check_answer_domain("求概率。", r"$\frac{1}{2}$")
        self.assertFalse(r["violation"])


class TestOperatorTailSignal(unittest.TestCase):
    def test_operator_tail_truncated(self):
        # 写满且以运算符结尾 → 表达式未写完
        s = ("a" * 16382) + " +"
        self.assertTrue(_looks_truncated(s, 16384))

    def test_clean_tail_not_truncated(self):
        s = ("a" * 16384)
        self.assertFalse(_looks_truncated(s, 16384))


if __name__ == "__main__":
    unittest.main()
