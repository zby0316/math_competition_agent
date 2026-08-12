# -*- coding: utf-8 -*-
"""V2 M3 DeepSolver 深思考通道测试。

覆盖：计划触发（超难领域/难度/客观题跳过）、多候选合并（多数一致/分歧取高置信/
单候选/无候选）。
"""
import unittest

from utils.deep_solver import deep_solver_plan, merge_deep_candidates


class TestDeepSolverPlan(unittest.TestCase):

    def test_hard_difficulty_triggers(self):
        plan = deep_solver_plan("computation", difficulty="hard")
        self.assertTrue(plan["enabled"])
        self.assertEqual(plan["candidates"], 3)
        self.assertTrue(plan["verify"])

    def test_deep_domain_triggers(self):
        plan = deep_solver_plan("computation", category="数论")
        self.assertTrue(plan["enabled"])

    def test_normal_question_skips(self):
        plan = deep_solver_plan("computation", category="数学分析", difficulty="medium")
        self.assertFalse(plan["enabled"])

    def test_objective_never_deep(self):
        plan = deep_solver_plan("choice", category="数论", difficulty="hard")
        self.assertFalse(plan["enabled"])
        plan2 = deep_solver_plan("true_false", difficulty="hard")
        self.assertFalse(plan2["enabled"])


class TestMergeDeepCandidates(unittest.TestCase):

    def test_majority_wins(self):
        cands = [
            {"answer": "7", "confidence": 0.8},
            {"answer": "7", "confidence": 0.9},
            {"answer": "9", "confidence": 0.95},
        ]
        ans, reason = merge_deep_candidates(cands)
        self.assertEqual(ans, "7")
        self.assertIn("候选一致", reason)

    def test_latex_equivalence_counts_as_same(self):
        # LaTeX 表达等价视为一致（$\\frac{1}{2}$ vs 1/2）
        cands = [
            {"answer": "\\frac{1}{2}", "confidence": 0.8},
            {"answer": "1/2", "confidence": 0.9},
            {"answer": "3", "confidence": 0.99},
        ]
        ans, reason = merge_deep_candidates(cands)
        self.assertEqual(ans, "\\frac{1}{2}")
        self.assertIn("候选一致", reason)

    def test_split_takes_highest_confidence(self):
        cands = [
            {"answer": "5", "confidence": 0.7},
            {"answer": "6", "confidence": 0.95},
            {"answer": "7", "confidence": 0.6},
        ]
        ans, reason = merge_deep_candidates(cands)
        self.assertEqual(ans, "6")
        self.assertIn("最高置信", reason)

    def test_single_candidate(self):
        ans, reason = merge_deep_candidates([{"answer": "42", "confidence": 0.9}])
        self.assertEqual(ans, "42")
        self.assertIn("单候选", reason)

    def test_all_invalid_returns_empty(self):
        ans, reason = merge_deep_candidates([
            {"answer": "无法求解", "confidence": 0.9},
            {"answer": "", "confidence": 0.8},
        ])
        self.assertEqual(ans, "")
        self.assertIn("无可用候选", reason)


if __name__ == "__main__":
    unittest.main()
