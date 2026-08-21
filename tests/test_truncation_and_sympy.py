# -*- coding: utf-8 -*-
"""V2.6.1/V2.6.2 截断检测与 sympy 预解器测试。

覆盖：finish_reason 可见性判定、完整长答案不误判（V2.6.1 修复）、
未闭合 LaTeX 强信号、sympy 自然语言识别/拒绝、多条件题不短路、
算术符号残留不短路、输出美化（*→·）、同余保留 x=。
"""
import unittest

from utils.llm_retry import _finish_reason_visible, _looks_truncated
from utils.sympy_pre_solver import SympyPreSolver


class TestTruncationHeuristic(unittest.TestCase):

    def test_complete_long_answer_not_flagged(self):
        # V2.6.1：完整长答案（结尾正常句号）不得被纯长度判据误判为截断
        filler = "步骤" * 3000  # 6000 字符，超过 4096*1.2
        text = "## 问题分析\n这是分析。\n## 详细解题步骤\n" + filler + "\n## 最终答案\n5。"
        self.assertFalse(_looks_truncated(text, 4096))

    def test_unclosed_latex_flagged(self):
        self.assertTrue(_looks_truncated("答案是 $x^2", 4096))

    def test_unbalanced_braces_flagged(self):
        self.assertTrue(_looks_truncated("答案 {x^2", 4096))

    def test_finish_reason_visible_dict(self):
        self.assertTrue(_finish_reason_visible(
            {"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]}))
        self.assertFalse(_finish_reason_visible(
            {"choices": [{"message": {"content": "x"}}]}))
        # 纯字符串 client：finish_reason 不可见 → 启发式启用
        self.assertFalse(_finish_reason_visible("plain content"))


class TestSympyPreSolver(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.solver = SympyPreSolver()

    def test_modular_power_cn(self):
        out = self.solver.pre_solve("求 2^100 除以 13 的余数")
        self.assertEqual(out["whole_answer"], "3")

    def test_congruence_keeps_x(self):
        # V2.6.1：同余解保留 "x=" 形态，不再只给裸数值
        out = self.solver.pre_solve("解同余方程 3x ≡ 7 (mod 11)，求 x。")
        self.assertIn("x=6", out["whole_answer"])

    def test_combination_eval(self):
        out = self.solver.pre_solve("求 C(10,5) 的值")
        self.assertEqual(out["whole_answer"], "252")

    def test_symbolic_arithmetic_not_whole_goal(self):
        # V2.6.1：算术结果含自由符号（"求 x 的值" 被算成 "x"）不得短路
        out = self.solver.pre_solve("求 x 的值")
        self.assertEqual(out["whole_answer"], "")

    def test_multi_condition_blocked(self):
        # V2.6.1：含"且"的多条件题 sympy 无法应用约束，不得短路
        out = self.solver.pre_solve("求满足 x^2=4 且 x>0 的 x")
        self.assertEqual(out["hints"], [])
        self.assertEqual(out["whole_answer"], "")

    def test_constraint_words_blocked(self):
        # V2.7.2 审计补强：单字约束词（正根/整数解等）无法机械应用——
        # "x²=4 的正根"若短路会把两个根全交出去并被确定性直通锁死。
        for problem in ("求方程 x^2=4 的正根", "求 x^2=2 的整数解",
                        "求方程 x^2=4 的非负解", "求 x^2=4 的实根"):
            out = self.solver.pre_solve(problem)
            self.assertEqual(out["whole_answer"], "", problem)

    def test_pretty_multiplication(self):
        # V2.6.2：输出美化 * → ·
        out = self.solver.pre_solve("求 x^2+1 的导数")
        self.assertEqual(out["whole_answer"], "2·x")

    def test_plain_text_rejected(self):
        # 无法机械解析的自然语言题 → 交回 LLM
        out = self.solver.pre_solve("证明每个有限群都存在一个 Sylow p-子群。")
        self.assertEqual(out["whole_answer"], "")


class TestSympyPreSolveNode(unittest.TestCase):
    """节点层守卫（V2.6.2：防 _reject_bad_answer 逻辑反转回归——该 bug 曾让
    干净结果全部被拒、短路功能整体失效，只有节点层测试能拦住）。"""

    @staticmethod
    def _node(problem, difficulty="easy"):
        from nodes.reasoning_agent_node import _sympy_pre_solve
        state = {"problem": problem, "question_mode": "computation",
                 "difficulty": difficulty}
        return _sympy_pre_solve(state, None)

    def test_clean_congruence_short_circuits(self):
        out = self._node("解同余方程 3x ≡ 7 (mod 11)，求 x。")
        self.assertIsNotNone(out)
        self.assertEqual(out["reasoning_result"]["answer"], "x=6 (mod 11)")
        self.assertEqual(out["reasoning_attempts"], 0)
        self.assertEqual(out["reasoning_result"]["answer_source"],
                         "sympy_pre_solve")

    def test_lerchphi_garbage_not_shorted(self):
        # 未定值泄漏（含自由符号 n + 特殊函数）不得短路
        out = self._node(r"求极限 $\lim_{n\to\infty} \int_0^1 \frac{x^n}{1+x}\,dx$。")
        self.assertIsNone(out)

    def test_trapezoid_approximation_not_shorted(self):
        # 近似算法题：sympy 只会算精确值（1/3），题面要梯形近似（3/8）→ 不短路
        out = self._node(r"用梯形公式（把 $[0,1]$ 二等分）计算 $\int_0^1 x^2\,dx$ 的近似值。")
        self.assertIsNone(out)

    def test_hard_difficulty_not_shorted(self):
        out = self._node("解同余方程 3x ≡ 7 (mod 11)，求 x。", difficulty="hard")
        self.assertIsNone(out)

    def test_multi_condition_not_shorted(self):
        out = self._node("求满足 x^2=4 且 x>0 的 x")
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
