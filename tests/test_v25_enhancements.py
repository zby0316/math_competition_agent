# -*- coding: utf-8 -*-
"""V2.5 增强验证：比值方向 + 三角表达式化简 + 解集守卫集成。"""
import unittest

from utils.answer_formatter import _simplify_trig_expression
from utils.form_align import _check_ratio_direction, check_form_alignment
from utils.solution_completeness_guard import (
    code_has_solution_enumeration, detect_solution_completeness, prompt_clause,
)


class TestTrigSimplify(unittest.TestCase):
    def test_idx103_expression_to_degrees(self):
        # easy112 idx=103 真实场景：数值精确 54° 的复合表达式
        expr = "180*acos(sqrt(sqrt(5) + 5)/(2*sqrt(sqrt(5) + 3)))/pi"
        self.assertEqual(_simplify_trig_expression(expr), "54°")

    def test_atan1(self):
        self.assertEqual(_simplify_trig_expression("atan(1)"), "45°")

    def test_irrelevant_unchanged(self):
        self.assertEqual(_simplify_trig_expression("x^2 + 1"), "x^2 + 1")
        self.assertEqual(_simplify_trig_expression("3/4"), "3/4")


class TestRatioDirection(unittest.TestCase):
    def test_reversed_letter_ratio_detected(self):
        ok, reason = _check_ratio_direction(
            "What is the ratio of apples to bananas?", "bananas:apples")
        self.assertFalse(ok)
        self.assertIn("颠倒", reason)

    def test_correct_direction_ok(self):
        ok, _ = _check_ratio_direction(
            "What is the ratio of apples to bananas?", "apples:bananas")
        self.assertTrue(ok)

    def test_no_ratio_ask_ok(self):
        ok, _ = _check_ratio_direction("求 x 的值", "16:9")
        self.assertTrue(ok)


class TestSolutionGuardIntegration(unittest.TestCase):
    def test_detect_and_clause(self):
        self.assertTrue(detect_solution_completeness(
            "Find all values of x with 0 <= x < 2pi that satisfy sin x + cos x = sqrt(2)."))
        self.assertIn("解集完整性强制条款", prompt_clause(
            "Find all values of x that satisfy sin x + cos x = sqrt(2)."))

    def test_code_enumeration(self):
        self.assertTrue(code_has_solution_enumeration(
            "import sympy\nx = sympy.Symbol('x')\nsol = sympy.solveset(x**2-4, x)\nprint(len(sol))"))
        self.assertFalse(code_has_solution_enumeration('print("最终答案: 2")'))


if __name__ == "__main__":
    unittest.main()
