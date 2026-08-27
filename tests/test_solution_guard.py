# -*- coding: utf-8 -*-
"""解集完整性守卫单元测试（V2.5，easy112 错题归因 P1）。"""
import unittest

from utils.solution_completeness_guard import (
    code_has_solution_enumeration, detect_solution_completeness, prompt_clause,
)


class TestDetect(unittest.TestCase):
    def test_find_all_english(self):
        self.assertTrue(detect_solution_completeness(
            "Find all values of x with 0 <= x < 2pi that satisfy sin x + cos x = sqrt(2)."))
        self.assertTrue(detect_solution_completeness(
            "Find all solutions to x^2 - 4 = 0."))

    def test_find_all_chinese(self):
        self.assertTrue(detect_solution_completeness("求方程 x^3-x=0 的所有实数根。"))
        self.assertTrue(detect_solution_completeness("列出所有满足条件的正整数。"))

    def test_min_guarantee(self):
        self.assertTrue(detect_solution_completeness(
            "What is the least number of gumballs that Wally must buy to guarantee "
            "that he receives 3 gumballs of the same colour?"))
        self.assertTrue(detect_solution_completeness("至少要买多少个才能保证有两个同色？"))

    def test_negative(self):
        self.assertFalse(detect_solution_completeness("求 x 的值，已知 2x+3=7。"))
        self.assertFalse(detect_solution_completeness("What is the area of the circle?"))
        self.assertFalse(detect_solution_completeness("计算 2+3 的值。"))


class TestCodeEnumeration(unittest.TestCase):
    def test_has_solve(self):
        self.assertTrue(code_has_solution_enumeration(
            "import sympy\nx = sympy.Symbol('x')\nsol = sympy.solveset(x**2-4, x)\nprint(len(sol))"))
        self.assertTrue(code_has_solution_enumeration(
            "from itertools import product\nfor a in range(10):\n    print(a)"))

    def test_no_enumeration(self):
        self.assertFalse(code_has_solution_enumeration(
            'print("最终答案: 2")'))
        self.assertFalse(code_has_solution_enumeration(""))


class TestPromptClause(unittest.TestCase):
    def test_hit_has_clause(self):
        self.assertIn("解集完整性强制条款", prompt_clause(
            "Find all values of x that satisfy sin x + cos x = sqrt(2)."))
        self.assertIn("鸽巢", prompt_clause(
            "至少要买多少个才能保证有两个同色？"))

    def test_miss_empty(self):
        self.assertEqual(prompt_clause("求 x 的值，已知 2x+3=7。"), "")


if __name__ == "__main__":
    unittest.main()
