# -*- coding: utf-8 -*-
"""确定性求解器矩阵 det_solver 的单元测试。

覆盖 15 条管线各至少一个正例与一个负例，重点用例采用历史真实错题场景
（hook length / 递推 / 模周期 / 特征值 / 组合数 / 错排 / 梯形法 / 最短路 /
留数 / ODE / 线性方程组 / 期望等），验证通用算法的正确性而非背答案。
所有断言必须由 `python -B -m unittest tests.test_det_solver -v` 实际通过。
"""
import unittest

from utils.det_solver import det_solve, det_solve_kinds


def _solve(problem):
    return det_solve(problem)


def _assert_solved(testcase, problem, kind=None):
    result = _solve(problem)
    testcase.assertTrue(result, "expected solved, got {} for: %s" % problem)
    testcase.assertIn("answer", result)
    testcase.assertIn("kind", result)
    testcase.assertIn("evidence", result)
    if kind is not None:
        testcase.assertEqual(result["kind"], kind)
    return result


class TestKinds(unittest.TestCase):
    def test_all_kinds_listed(self):
        kinds = det_solve_kinds()
        # V2.6 新增 number_theory / finite_field 两条管线
        self.assertEqual(len(kinds), 17)
        self.assertIn("modular_period", kinds)
        self.assertIn("number_theory", kinds)
        self.assertIn("finite_field", kinds)
        self.assertIn("bvp_shoot", kinds)
        # 顺序：modular_period 在前，bvp_shoot 在最后
        self.assertEqual(kinds[0], "modular_period")
        self.assertEqual(kinds[-1], "bvp_shoot")


class TestNumberTheoryV26(unittest.TestCase):
    """V2.6 数论增强管线（factorint/φ/CRT/gcd/lcm/二次剩余/因数个数）。"""

    def _solved(self, problem, answer, kind="number_theory"):
        r = det_solve(problem)
        self.assertTrue(r, "expected solved, got None for: %s" % problem)
        self.assertEqual(r["kind"], kind)
        self.assertEqual(r["answer"], answer)

    def test_divisor_count(self):
        self._solved("360 的正因数个数", "24")
        self._solved("求 360 的因数个数", "24")

    def test_divisor_sum(self):
        self._solved("2024 的正因数之和", "4320")

    def test_totient(self):
        self._solved("φ(100) 的值", "40")
        self._solved("与 36 互素的正整数个数", "12")

    def test_isprime(self):
        self._solved("1000003 是质数吗", "是")
        self._solved("1000005 是质数吗", "否")

    def test_factorint(self):
        self._solved("360 的质因数分解", "2^3 · 3^2 · 5")

    def test_gcd_lcm(self):
        self._solved("gcd(84, 126)", "42")
        self._solved("lcm(12, 18)", "36")

    def test_crt(self):
        self._solved("x ≡ 2 (mod 3), x ≡ 3 (mod 5) 的解", "x = 8 (mod 15)")

    def test_legendre(self):
        self._solved("5 是否为 23 的二次剩余", "否")
        self._solved("2 是否为 23 的二次剩余", "是")

    def test_unit_group(self):
        self._solved("Z_20 的单位群元素个数", "8")

    def test_guard_no_hijack(self):
        # 问目标守卫负例：题面出现数论记号但问别的 / 证明题 / 后续运算
        self.assertFalse(det_solve("证明 360 的因数个数是 24"))
        self.assertFalse(det_solve("求 gcd(84,126) 与 360 的差"))
        self.assertFalse(det_solve("求 360 的因数个数的一半"))
        self.assertFalse(det_solve("求 x ≡ 2 (mod 3), x ≡ 3 (mod 5) 的解与 100 的差"))


class TestFiniteFieldV26(unittest.TestCase):
    """V2.6 有限域管线（生成元计数/子域个数/F_2 子空间计数）。"""

    def test_field_generator_count_official_sample(self):
        # 官方本地样例题：F_81 = F_3(α) 的 α 个数 = 81 − 9 = 72
        prob_zh = ("设 $\\mathbb{F}_{81}$ 为 81 元的有限域。$T=\\{\\alpha\\in\\mathbb{F}_{81}"
                   "|\\mathbb{F}_{81}=\\mathbb{F}_3(\\alpha)\\}$。求 $T$ 中元素的个数")
        r = det_solve(prob_zh)
        self.assertTrue(r, "expected solved")
        self.assertEqual(r["kind"], "finite_field")
        self.assertEqual(r["answer"], "72")
        prob_en = ("Let $\\mathbb{F}_{81}$ be the finite field with 81 elements. Let T be "
                   "the set of $\\alpha$ in $\\mathbb{F}_{81}$ such that "
                   "$\\mathbb{F}_{81}=\\mathbb{F}_3(\\alpha)$. Find the number of elements of T.")
        r = det_solve(prob_en)
        self.assertTrue(r, "expected solved (en)")
        self.assertEqual(r["answer"], "72")

    def test_subfield_count(self):
        r = det_solve("F_16 的子域个数")
        self.assertTrue(r)
        self.assertEqual(r["kind"], "finite_field")
        self.assertEqual(r["answer"], "3")

    def test_gaussian_binomial(self):
        r = det_solve("F_2 上 4 维向量空间的 2 维子空间个数")
        self.assertTrue(r)
        self.assertEqual(r["kind"], "finite_field")
        self.assertEqual(r["answer"], "35")

    def test_guard_proof_no_hijack(self):
        self.assertFalse(det_solve("设 F_81 为 81 元有限域，证明其乘法群是循环群"))


class TestModularPeriod(unittest.TestCase):
    def test_power_order_positive(self):
        r = _assert_solved(self, "求 2^n mod 13 的周期。", "modular_period")
        self.assertEqual(r["answer"], "12")

    def test_fibonacci_pisano_positive(self):
        r = _assert_solved(self, "求斐波那契数列 mod 10 的最小正周期。", "modular_period")
        self.assertEqual(r["answer"], "60")

    def test_negative(self):
        self.assertEqual(_solve("求斐波那契数列的第 20 项。"), {})


class TestLinearRecurrence(unittest.TestCase):
    def test_second_order_positive(self):
        r = _assert_solved(self, "a_n = 2a_{n-1} + 3a_{n-2}, a_0=0, a_1=1，求 a_10 的值。",
                           "linear_recurrence")
        self.assertEqual(r["answer"], "a(10) = 14762")

    def test_negative(self):
        self.assertEqual(_solve("求数列 a_n 的第 5 项。"), {})


class TestPartitionsHook(unittest.TestCase):
    def test_s6_positive(self):
        r = _assert_solved(self, "求对称群 S6 的全部不可约表示的维数。", "partitions_hook")
        self.assertEqual(r["answer"], "[1,1,5,5,5,5,9,9,10,10,16]")
        self.assertIn("720", r["evidence"])

    def test_negative(self):
        # n 超过 15 → 保守放弃
        self.assertEqual(_solve("求对称群 S20 的全部不可约表示的维数。"), {})


class TestCountCombinatorics(unittest.TestCase):
    def test_binomial_positive(self):
        r = _assert_solved(self, "求组合数 C(10,3) 的值。", "count_combinatorics")
        self.assertEqual(r["answer"], "C(10,3) = 120")

    def test_derangement_positive(self):
        r = _assert_solved(self, "求 5 个元素的错排数。", "count_combinatorics")
        self.assertEqual(r["answer"], "!5 = 44")

    def test_negative(self):
        self.assertEqual(_solve("求组合数 C(n,k) 的值。"), {})

    def test_binomial_statement_not_hijacked(self):
        # V2.7.3 问目标守卫回归（官方 54.46 回溯根因）：题面陈述里顺带出现
        # C(n,k)、但题问的是别的（概率）——绝不劫持作答组合数。
        self.assertEqual(_solve(
            "集合 {1,2,...,10} 的 3 元子集共有 C(10,3) 个，从中等可能任取一个，"
            "求该子集包含元素 1 的概率。"), {})


class TestMatrixEigen(unittest.TestCase):
    def test_positive(self):
        r = _assert_solved(self, "求矩阵 [[4,1],[2,3]] 的特征值与特征向量。", "matrix_eigen")
        self.assertIn("5", r["answer"])
        self.assertIn("2", r["answer"])

    def test_negative(self):
        # 非方阵 → 无法求特征值
        self.assertEqual(_solve("求矩阵 [[1,2],[3,4],[5,6]] 的特征值。"), {})

    def test_diagonalizable_judgement_not_hijacked(self):
        # V2.7.3 问目标守卫回归：问判定/参数条件，不是特征值列表
        self.assertEqual(_solve("判断矩阵 [[4,1],[2,3]] 是否可对角化。"), {})
        self.assertEqual(_solve(
            "求使得矩阵 A = [[2, a], [0, 2]] 可对角化的实数 a 的所有取值。"), {})


class TestLinearSystem(unittest.TestCase):
    def test_positive(self):
        r = _assert_solved(self, "解方程组 x+y=3, x-y=1，求 x 和 y。", "linear_system")
        self.assertIn("x = 2", r["answer"])
        self.assertIn("y = 1", r["answer"])

    def test_negative(self):
        self.assertEqual(_solve("解方程 x+y=3。"), {})

    def test_derived_expression_not_hijacked(self):
        # V2.7.3 问目标守卫回归：问的是导出表达式 2x+y，不是变量本身
        self.assertEqual(_solve(
            "解方程组 x+y=3, x-y=1，求 2x+y 的值。"), {})

    def test_variable_ask_still_hits(self):
        r = _assert_solved(self, "解方程组 x+y=3, x-y=1，求 x、y 的值。",
                           "linear_system")
        self.assertIn("x = 2", r["answer"])
        self.assertIn("y = 1", r["answer"])


class TestQuadrature(unittest.TestCase):
    def test_trapezoid_positive(self):
        r = _assert_solved(self, "用梯形公式（把 [0,1] 二等分）计算 ∫_0^1 x² dx 的近似值。",
                           "quadrature")
        # V2.7.2 judger 对齐：分数+小数双形式（官方判分实证只给小数判 partial）
        self.assertIn("3/8", r["answer"])
        self.assertIn("0.375", r["answer"])

    def test_negative(self):
        # 缺等分数 n
        self.assertEqual(_solve("用梯形公式计算 ∫_0^1 x^2 dx 的近似值。"), {})


class TestGraphOpt(unittest.TestCase):
    def test_shortest_path_positive(self):
        r = _assert_solved(self, "求最短路：顶点 A、B、C，边 AB=1，BC=2，AC=4，"
                                 "求 A 到 C 的最短路径长度。", "graph_opt")
        self.assertEqual(r["answer"], "3")

    def test_negative(self):
        self.assertEqual(_solve("求一个连通图的顶点个数。"), {})


class TestProbabilityExact(unittest.TestCase):
    def test_dice_sum_positive(self):
        r = _assert_solved(self, "掷两颗骰子，求点数和为 7 的概率。", "probability_exact")
        self.assertIn("1/6", r["answer"])

    def test_negative(self):
        self.assertEqual(_solve("掷两颗骰子。"), {})


class TestPolyInterp(unittest.TestCase):
    def test_positive(self):
        r = _assert_solved(self, "已知 f(0)=1, f(1)=2, f(2)=5，用拉格朗日插值求 f(x)。",
                           "poly_interp")
        self.assertIn("x^2 + 1", r["answer"])

    def test_negative(self):
        self.assertEqual(_solve("用拉格朗日插值法。"), {})

    def test_value_at_point(self):
        # V2.7 真机事故回归：题目要"x=0.5 处的值"时不能只答多项式本体
        r = _assert_solved(
            self,
            "已知二次插值多项式经过点 (0,1)、(1,3)、(2,9)，求该多项式在 x=0.5 处的值。",
            "poly_interp")
        self.assertIn("3/2", r["answer"])

    def test_polynomial_question_keeps_polynomial(self):
        # 求 f(x) 时不得被题面里的数据点 f(0)=1 误导成求值
        r = _assert_solved(
            self,
            "已知 f(0)=1, f(1)=2, f(2)=5，用拉格朗日插值求 f(x)。",
            "poly_interp")
        self.assertIn("f(x)", r["answer"])

    def test_f_value_question_with_data_points(self):
        # "求 f(2) 的值"（f(2)=5 是数据点）→ 应返回数值 5
        r = _assert_solved(
            self,
            "已知 f(0)=1, f(1)=2, f(2)=5，用拉格朗日插值法求 f(2) 的值。",
            "poly_interp")
        self.assertIn("5", r["answer"])


class TestSeriesCoeff(unittest.TestCase):
    def test_positive(self):
        r = _assert_solved(self, "求 sin(x) 在 x=0 处的泰勒展开中 x^3 的系数。", "series_coeff")
        self.assertEqual(r["answer"], "-1/6")

    def test_negative(self):
        self.assertEqual(_solve("求 sin(x) 的泰勒展开式。"), {})


class TestExpectationVar(unittest.TestCase):
    def test_dice_positive(self):
        r = _assert_solved(self, "掷一颗骰子，求点数的期望。", "expectation_var")
        self.assertIn("3.5", r["answer"])

    def test_negative(self):
        self.assertEqual(_solve("求随机变量的期望。"), {})


class TestResidueComplex(unittest.TestCase):
    def test_positive(self):
        r = _assert_solved(self, "求函数 e^z/z^2 在 z=0 处的留数。", "residue_complex")
        self.assertEqual(r["answer"], "1")

    def test_negative(self):
        self.assertEqual(_solve("求函数 f(z) 的留数。"), {})


class TestOdeIvp(unittest.TestCase):
    def test_positive(self):
        r = _assert_solved(self, "y'=y, y(0)=1，求 y(1) 的值。", "ode_ivp")
        self.assertIn("2.71828", r["answer"])

    def test_negative(self):
        self.assertEqual(_solve("y'=y。"), {})


class TestBvpShoot(unittest.TestCase):
    def test_positive(self):
        r = _assert_solved(self, "求边值问题 y''=-y，y(0)=0，y(1)=sin(1)，求 y(0.5) 的值。",
                           "bvp_shoot")
        self.assertIn("0.4794255", r["answer"])

    def test_negative(self):
        self.assertEqual(_solve("求边值问题 y''=-y，y(0)=0。"), {})


class TestNonComputational(unittest.TestCase):
    def test_proof_returns_empty(self):
        self.assertEqual(_solve("证明欧拉公式 e^{iπ}+1=0。"), {})

    def test_explain_returns_empty(self):
        self.assertEqual(_solve("解释什么是群。"), {})

    def test_plain_text_returns_empty(self):
        self.assertEqual(_solve("请写出你的解题思路。"), {})

    def test_no_throw_on_empty(self):
        self.assertEqual(_solve(""), {})

    def test_unbraced_frac_no_deadloop(self):
        # V2.7.2 审计修复回归：无花括号/不平衡的 \frac 曾让 _latex2sym 的
        # while 死循环（无超时兜底可卡到节点硬超时）。必须立即返回不悬挂。
        from utils.det_solver import _latex2sym
        for evil in (r"\frac12", r"\frac{1}{2", r"\frac x2"):
            out = _latex2sym(evil)
            self.assertIsInstance(out, str)  # 正常返回即未死循环


class TestPartitionsHookQuestionMismatch(unittest.TestCase):
    """问非所答型误触发守卫（V2.7 补充）：对称群语境 + 非维数问题不得触发。"""

    def test_conjugacy_class_count_skips(self):
        self.assertEqual(_solve("求对称群 S6 的共轭类个数。"), {})

    def test_group_order_skips(self):
        self.assertEqual(_solve("求对称群 S6 的阶。"), {})

    def test_center_skips(self):
        self.assertEqual(_solve("求 S5 的中心。"), {})

    def test_partition_count_without_group_skips(self):
        # 整数分拆**个数** p(n) 与 hook 维数列表是不同的问题
        self.assertEqual(_solve("求 6 的整数分拆数。"), {})

    def test_dim_question_still_hits(self):
        r = _assert_solved(self, "求对称群 S6 的全部不可约表示的维数。",
                           "partitions_hook")
        self.assertEqual(r["answer"], "[1,1,5,5,5,5,9,9,10,10,16]")


if __name__ == "__main__":
    unittest.main()
