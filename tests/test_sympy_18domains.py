# -*- coding: utf-8 -*-
"""V2.5 sympy_pre_solver 18 领域扩展测试（9 个新类别）。"""
import unittest

from utils.sympy_pre_solver import SympyPreSolver

_S = SympyPreSolver()


def _whole(problem):
    r = _S.pre_solve(problem)
    return (r or {}).get("whole_answer", "")


class TestGcdLcm(unittest.TestCase):
    def test_gcd(self):
        self.assertIn("gcd(48,36)", _whole("求 48 和 36 的最大公约数 (gcd)。"))
        self.assertEqual(_whole("求 gcd(48,36) 的值。"), "gcd(48,36) = 12")
        self.assertIn("12", _whole("求 gcd(48,36) 的值。"))

    def test_lcm(self):
        self.assertIn("24", _whole("求 lcm(8,12) 的值。"))

    def test_negative(self):
        self.assertEqual(_whole("求 x 的值。"), "")


class TestModularInverse(unittest.TestCase):
    def test_inverse(self):
        r = _whole("求 3 模 7 的逆元。")
        self.assertIn("5", r)

    def test_no_inverse(self):
        r = _whole("求 4 模 8 的逆元。")
        self.assertIn("无模逆", r)

    def test_negative(self):
        self.assertEqual(_whole("求 3+4 的值。"), "")


class TestStatistics(unittest.TestCase):
    def test_mean(self):
        self.assertIn("3", _whole("求列表 [1, 2, 3, 4, 5] 的平均值。"))

    def test_median(self):
        r = _whole("求数据 [1, 3, 5, 7] 的中位数。")
        self.assertIn("4", r)  # 偶数个取中间两数平均

    def test_variance(self):
        self.assertIn("2", _whole("求数据 [1, 2, 3, 4, 5] 的方差。"))

    def test_std(self):
        r = _whole("求数据 [1, 2, 3, 4, 5] 的标准差。")
        self.assertIn("1.414", r)  # sqrt(2) 数值

    def test_negative(self):
        self.assertEqual(_whole("求面积。"), "")


class TestLinearRegression(unittest.TestCase):
    def test_slope(self):
        r = _whole("对数据点 (1,2),(2,3),(3,5) 做最小二乘回归，求斜率。")
        self.assertIn("3/2", r)  # 1.5 的分数形态

    def test_full(self):
        r = _whole("对数据点 (1,2),(2,3),(3,5) 做线性回归。")
        self.assertIn("y =", r)

    def test_negative(self):
        self.assertEqual(_whole("求圆的面积。"), "")


class TestMatrixTarget(unittest.TestCase):
    def test_det(self):
        r = _whole(r"求矩阵 $\begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}$ 的行列式。")
        self.assertIn("-2", r)

    def test_rank(self):
        r = _whole(r"求矩阵 $\begin{pmatrix} 1 & 2 \\ 2 & 4 \end{pmatrix}$ 的秩。")
        self.assertIn("1", r)

    def test_negative(self):
        self.assertEqual(_whole("求矩阵的特征向量。"), "")


class TestComplexOps(unittest.TestCase):
    def test_abs(self):
        r = _whole("求复数 3+4i 的模。")
        self.assertIn("5", r)

    def test_conj(self):
        r = _whole("求复数 3+4i 的共轭。")
        self.assertIn("3", r)

    def test_negative(self):
        self.assertEqual(_whole("求 x^2 的积分。"), "")


class TestVectorOps(unittest.TestCase):
    def test_dot(self):
        r = _whole("求向量 (1,2,3) 与 (4,5,6) 的点积。")
        self.assertIn("32", r)

    def test_cross(self):
        r = _whole("求向量 (1,0,0) 与 (0,1,0) 的叉积。")
        self.assertIn("1", r)  # 结果 (0, 0, 1)

    def test_angle(self):
        r = _whole("求向量 (1,0) 与 (0,1) 的夹角。")
        self.assertIn("90", r)

    def test_negative(self):
        self.assertEqual(_whole("求两点间距离。"), "")


class TestSeriesSum(unittest.TestCase):
    def test_arithmetic(self):
        r = _whole("求首项为 1、公差为 2 的等差数列前 5 项和。")
        self.assertIn("25", r)

    def test_negative(self):
        self.assertEqual(_whole("求数列的极限。"), "")


class TestUnicodeIntegral(unittest.TestCase):
    """V2.5 扩展：Unicode ∫ 形态（官方题面常见）。"""

    def test_definite_unicode(self):
        r = _whole("计算定积分 ∫_0^1 x^2 dx。")
        self.assertIn("1/3", r)

    def test_latex_definite(self):
        r = _whole(r"计算定积分 $\int_0^1 x^2 dx$。")
        self.assertIn("1/3", r)


class TestPythonListMatrix(unittest.TestCase):
    """V2.5 扩展：Python 列表矩阵格式。"""

    def test_det_python_list(self):
        r = _whole("求矩阵 [[1,2],[3,4]] 的行列式。")
        self.assertIn("-2", r)

    def test_rank_python_list(self):
        r = _whole("求矩阵 [[1,2],[2,4]] 的秩。")
        self.assertIn("1", r)


class TestVectorNorm(unittest.TestCase):
    """V2.5 扩展：向量模长（泛函分析/微分几何）。"""

    def test_norm(self):
        r = _whole("求向量 (3,4) 的模长。")
        self.assertIn("5", r)

    def test_norm_3d(self):
        r = _whole("求向量 (1,2,2) 的模长。")
        self.assertIn("3", r)


class TestMultivariatePartial(unittest.TestCase):
    """V2.5 扩展：多元函数偏导数。"""

    def test_partial_u(self):
        r = _whole("求函数 u(x,y)=x^2+y^2 关于 x 的偏导数。")
        self.assertIn("2*x", r)

    def test_partial_f(self):
        r = _whole("求 f(x,y)=x*y^2 关于 y 的偏导数。")
        self.assertIn("2*x*y", r)


class TestOdeFirstOrder(unittest.TestCase):
    def test_y_prime_equals_f(self):
        r = _whole("求微分方程 y' = 2x 的通解。")
        self.assertIn("x^2", r)

    def test_linear_ode(self):
        r = _whole("求微分方程 y' + 2y = 0 的通解。")
        self.assertIn("C", r)

    def test_negative(self):
        self.assertEqual(_whole("求圆的面积。"), "")


if __name__ == "__main__":
    unittest.main()
