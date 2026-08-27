"""sympy 事前直接解（V2.5a 主线 1）：确定性计算前置，能解就跳过 LLM。

移植榜一（Shusheng_ChallengeCup_2026）tools/sympy_tool.py 的 hints_for 思路：
只处理**无歧义、可机械解析**的数学形式——算术、同余 ax≡b(mod m)、模幂 a^b mod m、
导数、积分（定/不定）、极限、方程、矩阵。解析不了或算不出就返回空，交给 LLM。

设计原则（与榜一一致）：
- 确定性计算：sympy 是纯符号计算，进程内直接调 API（无 subprocess 开销、无副作用）。
- 保守：只做有把握的式子，任何异常/解析失败返回空，绝不硬来。
- 优雅降级：sympy ImportError 时整体失效，交回 LLM。

与榜一的差异：返回结构化结果（hints + whole_answer），而非带 label 的字符串，
便于 reasoning 节点直接判断"能否跳过 LLM"。
"""

from __future__ import annotations

import re
from math import gcd
from typing import Any, Optional


#: "完整目标"类操作：sympy 解出这些通常是题目的最终答案（区别于中间子表达式）。
_WHOLE_GOAL_OPERATIONS = frozenset({
    "equation_solve", "definite_integral", "integral", "limit",
    "derivative", "congruence", "modular_power", "evaluate",
})


class SympyPreSolver:
    def __init__(self) -> None:
        try:
            import sympy as sympy_module
        except ImportError:
            sympy_module = None
        self.sympy = sympy_module

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def pre_solve(self, problem: str) -> dict:
        """识别无歧义数学子问题并确定性计算。

        返回 {"hints": [{"operation", "result", "whole_goal"}], "whole_answer": str|""}。
        ``whole_answer`` 是首个"完整目标"类操作的确定性结果（单问题可直接作答）。
        任何失败都返回空 hints，绝不抛异常。
        """
        if not self.sympy:
            return {"hints": [], "whole_answer": ""}
        try:
            hints = self.hints_for(str(problem or ""))
        except Exception:  # noqa: BLE001 - 确定性工具失败交回 LLM
            return {"hints": [], "whole_answer": ""}
        whole_answer = ""
        for hint in hints:
            if hint.get("whole_goal") and hint.get("result"):
                whole_answer = hint["result"]
                break
        return {"hints": hints, "whole_answer": whole_answer}

    # ------------------------------------------------------------------
    # 识别 + 分发（移植榜一 hints_for）
    # ------------------------------------------------------------------
    def hints_for(self, problem: str) -> list[dict]:
        hints: list[dict] = []

        # 同余 / 模幂（纯整数，最确定，优先）
        congruence = self._congruence(problem)
        if congruence:
            hints.append({"operation": "congruence", "result": congruence, "whole_goal": True})
        modular_power = self._modular_power(problem)
        if modular_power:
            hints.append({"operation": "modular_power", "result": modular_power, "whole_goal": True})

        # 算术求值（无积分/导数/极限/方程关键字时）
        arithmetic = re.search(
            r"(?:计算|求值|calculate|evaluate)\s*([0-9A-Za-z_+\-*/^().,\s]+?)[。？?]?$",
            problem, re.IGNORECASE)
        if arithmetic and not re.search(
                r"积分|导数|极限|方程|integral|derivative|limit|equation", problem, re.IGNORECASE):
            result = self._evaluate(arithmetic.group(1))
            if result is not None:
                hints.append({"operation": "evaluate", "result": result, "whole_goal": True})

        math_parts = self._math_parts(problem)

        # 积分（LaTeX \int 与 Unicode ∫ 双兼容）
        if re.search(r"积分|\\int|∫|integral|integrate", problem, re.IGNORECASE):
            for part in math_parts:
                # Unicode 形态: ∫_0^1 x^2 dx 或 ∫0^1 x^2 dx 或 ∫ x^2 dx
                # 下界/上界分开提取：下界 [^}\s^] 防止吞掉 ^上界
                definite_uni = re.search(
                    r"∫\s*(?:_\{?([^}\s^]+)\}?)?\s*(?:\^\{?([^}\s]+)\}?)?\s*(.+?)(?:\\,|\s)*d([A-Za-z])\b",
                    part)
                if definite_uni:
                    lower = definite_uni.group(1) or ""
                    upper = definite_uni.group(2) or ""
                    if lower and upper:
                        result = self._definite_integral(
                            self._latex_to_sympy(definite_uni.group(3)), definite_uni.group(4),
                            self._latex_to_sympy(lower), self._latex_to_sympy(upper))
                        if result is not None:
                            hints.append({"operation": "definite_integral", "result": result, "whole_goal": True})
                        break
                definite = re.search(
                    r"\\int_\{?([^}\s]+)\}?\^\{?([^}\s]+)\}?\s*(.+?)(?:\\,|\s)*d([A-Za-z])\b", part)
                if definite:
                    result = self._definite_integral(
                        self._latex_to_sympy(definite.group(3)), definite.group(4),
                        self._latex_to_sympy(definite.group(1)), self._latex_to_sympy(definite.group(2)))
                    if result is not None:
                        hints.append({"operation": "definite_integral", "result": result, "whole_goal": True})
                    break
                match = re.search(r"\\int\s*(.+?)(?:\\,|\s)*d([A-Za-z])\b", part)
                if match:
                    result = self._integral(self._latex_to_sympy(match.group(1)), match.group(2))
                    if result is not None:
                        hints.append({"operation": "integral", "result": result, "whole_goal": True})
                    break

        # 极限
        if re.search(r"极限|\\lim|\blimit\b", problem, re.IGNORECASE):
            for part in math_parts:
                match = re.search(r"\\lim_\{?\s*([A-Za-z])\s*\\to\s*([^}\s]+)\}?\s*(.+)", part)
                if match:
                    result = self._limit(self._latex_to_sympy(match.group(3)), match.group(1),
                                         self._latex_to_sympy(match.group(2)))
                    if result is not None:
                        hints.append({"operation": "limit", "result": result, "whole_goal": True})
                    break

        # 导数
        if re.search(r"导数|求导|微分|derivative|differentiate", problem, re.IGNORECASE):
            partial = re.search(
                r"f\s*\(\s*[A-Za-z]\s*,\s*[A-Za-z]\s*\)\s*=\s*(?P<expr>[^，。；;]+?)\s*关于\s*\$?(?P<var>[A-Za-z])\$?\s*的?(?:偏导|导数)", problem)
            # V2.5 扩展：任意多元函数 u(x,y)/f(x,y) 的偏导数（偏微分方程/数学分析）
            if not partial:
                partial = re.search(
                    r"(?:u|f|g|h|z)\s*\(\s*[A-Za-z]\s*,\s*[A-Za-z]\s*\)\s*=\s*(?P<expr>[^，。；;]+?)\s*关于\s*\$?(?P<var>[A-Za-z])\$?\s*的?(?:偏导|导数|偏微分)", problem)
            match = partial or re.search(
                r"(?:f\s*\(\s*(?P<var>[A-Za-z])\s*\)|y)\s*=\s*(?P<expr>[^，。；;]+?)(?=\s*(?:的(?:导数|微分)|[,，。；;]|$))", problem)
            if match:
                variable = match.group("var") or "x"
                result = self._derivative(self._latex_to_sympy(match.group("expr")), variable)
                if result is not None:
                    hints.append({"operation": "derivative", "result": result, "whole_goal": True})

        # 方程
        if re.search(r"方程|求解|equation|solve|roots?|zeros?", problem, re.IGNORECASE):
            for part in math_parts:
                if "=" not in part or r"\begin" in part:
                    continue
                left, right = part.split("=", 1)
                variable = re.search(r"\b([xyz])\b", left + right)
                if variable:
                    result = self._solve_equation(
                        f"({self._latex_to_sympy(left)})-({self._latex_to_sympy(right)})",
                        variable.group(1))
                    if result is not None:
                        if result:
                            answer = "，".join(f"{variable.group(1)}={item}" for item in result)
                        else:
                            answer = "无解"
                        hints.append({"operation": "equation_solve", "result": answer, "whole_goal": True})
                    break

        # 矩阵（只做解析，不解具体目标——矩阵题通常要行列式/特征值，交给 LLM）
        if re.search(r"矩阵|\\begin\{[pb]?matrix\}|\bmatrix\b", problem, re.IGNORECASE):
            for part in math_parts:
                match = re.search(r"\\begin\{[pb]?matrix\}(.+?)\\end\{[pb]?matrix\}", part, re.DOTALL)
                if match:
                    rows = [
                        [self._latex_to_sympy(cell) for cell in row.split("&")]
                        for row in re.split(r"\\\\", match.group(1))
                    ]
                    result = self._matrix(rows)
                    if result is not None:
                        hints.append({"operation": "matrix", "result": result, "whole_goal": False})
                    break

        # ---- V2.5 扩展：18 领域确定性覆盖（与 det_solver 互补，不重复） ----

        # 1) 数论：gcd / lcm（抽象代数/离散数学）
        gcd_lcm = self._gcd_lcm(problem)
        if gcd_lcm:
            hints.append({"operation": "gcd_lcm", "result": gcd_lcm, "whole_goal": True})

        # 2) 数论：模逆 a^{-1} mod m（抽象代数）
        mod_inv = self._modular_inverse(problem)
        if mod_inv:
            hints.append({"operation": "modular_inverse", "result": mod_inv, "whole_goal": True})

        # 3) 统计推断：均值/中位数/方差/标准差/极差（数据列表）
        stats = self._statistics(problem)
        if stats:
            hints.append({"operation": "statistics", "result": stats, "whole_goal": True})

        # 4) 线性回归：最小二乘斜率/截距（给定数据点）
        lr = self._linear_regression(problem)
        if lr:
            hints.append({"operation": "linear_regression", "result": lr, "whole_goal": True})

        # 5) 高等代数：矩阵目标运算（行列式/逆/秩/迹/转置）
        mtarget = self._matrix_target(problem)
        if mtarget:
            hints.append({"operation": "matrix_target", "result": mtarget, "whole_goal": True})

        # 6) 复分析：|z| / 共轭 / 实部 / 虚部
        zops = self._complex_ops(problem)
        if zops:
            hints.append({"operation": "complex_ops", "result": zops, "whole_goal": True})

        # 7) 微分几何/泛函：向量点积/叉积/模长/夹角
        vops = self._vector_ops(problem)
        if vops:
            hints.append({"operation": "vector_ops", "result": vops, "whole_goal": True})

        # 8) 数学分析/离散：等差/等比数列求和
        ssum = self._series_sum(problem)
        if ssum:
            hints.append({"operation": "series_sum", "result": ssum, "whole_goal": True})

        # 9) 常微分方程：一阶线性/可分离 ODE 显式解
        ode = self._ode_first_order(problem)
        if ode:
            hints.append({"operation": "ode_first_order", "result": ode, "whole_goal": True})

        return hints

    # ------------------------------------------------------------------
    # sympy 原子操作
    # ------------------------------------------------------------------
    def _evaluate(self, expression: str) -> Optional[str]:
        return self._run(lambda _: self._parse(expression))

    def _derivative(self, expression: str, variable: str = "x") -> Optional[str]:
        return self._run(lambda s: s.diff(self._parse(expression), s.Symbol(variable)))

    def _integral(self, expression: str, variable: str = "x") -> Optional[str]:
        return self._run(lambda s: s.integrate(self._parse(expression), s.Symbol(variable)))

    def _definite_integral(self, expression: str, variable: str, lower: str, upper: str) -> Optional[str]:
        return self._run(
            lambda s: s.integrate(self._parse(expression),
                                  (s.Symbol(variable), self._parse(lower), self._parse(upper))))

    def _limit(self, expression: str, variable: str, point: str) -> Optional[str]:
        return self._run(lambda s: s.limit(self._parse(expression), s.Symbol(variable), self._parse(point)))

    def _solve_equation(self, expression: str, variable: str = "x") -> Optional[list]:
        if not self.sympy:
            return None
        try:
            return [self._format(item)
                    for item in self.sympy.solve(self._parse(expression), self.sympy.Symbol(variable))]
        except Exception:
            return None

    def _matrix(self, rows: list) -> Optional[list]:
        if not self.sympy:
            return None
        try:
            return [[self._format(item) for item in row] for row in self.sympy.Matrix(rows).tolist()]
        except Exception:
            return None

    # ------------------------------------------------------------------
    # V2.5 扩展：18 领域确定性覆盖（与 det_solver 互补）
    # ------------------------------------------------------------------

    # 1) gcd / lcm（抽象代数/离散数学）
    def _gcd_lcm(self, problem: str) -> Optional[str]:
        if not re.search(r"最大公约数|最小公倍数|gcd|lcm", problem, re.IGNORECASE):
            return None
        m = re.search(r"(?:gcd|最大公约数)\s*[（(]?\s*(\d+)\s*[,，]\s*(\d+)", problem, re.IGNORECASE) \
            or re.search(r"(\d+)\s*和\s*(\d+)\s*的(?:gcd|最大公约数)", problem, re.IGNORECASE)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            return f"gcd({a},{b}) = {self._run(lambda s: s.gcd(a, b))}"
        m = re.search(r"(?:lcm|最小公倍数)\s*[（(]?\s*(\d+)\s*[,，]\s*(\d+)", problem, re.IGNORECASE) \
            or re.search(r"(\d+)\s*和\s*(\d+)\s*的(?:lcm|最小公倍数)", problem, re.IGNORECASE)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            return f"lcm({a},{b}) = {self._run(lambda s: s.lcm(a, b))}"
        return None

    # 2) 模逆 a^{-1} mod m（抽象代数/数论）
    def _modular_inverse(self, problem: str) -> Optional[str]:
        if not re.search(r"逆元|模逆|inverse|^{-1}", problem, re.IGNORECASE):
            return None
        m = re.search(r"(\d+)\s*(?:\^\{?-1\}|的?逆元)\s*(?:mod|模)\s*(\d+)", problem, re.IGNORECASE)
        if not m:
            m = re.search(r"(\d+)\s*(?:mod|模)\s*(\d+)\s*的?(?:逆元|inverse)", problem, re.IGNORECASE)
        if not m:
            return None
        a, mod = int(m.group(1)), int(m.group(2))
        if mod <= 0:
            return None
        try:
            inv = pow(a % mod, -1, mod)
            return f"{a} 模 {mod} 的逆元 = {inv}"
        except ValueError:
            return f"{a} 与 {mod} 不互素，无模逆"
        except Exception:
            return None

    # 3) 统计推断：均值/中位数/方差/标准差/极差（数据列表）
    def _statistics(self, problem: str) -> Optional[str]:
        want = None
        if re.search(r"平均|mean|average", problem, re.IGNORECASE):
            want = "mean"
        elif re.search(r"中位数|median", problem, re.IGNORECASE):
            want = "median"
        elif re.search(r"方差|variance", problem, re.IGNORECASE):
            want = "variance"
        elif re.search(r"标准差|standard deviation|std", problem, re.IGNORECASE):
            want = "std"
        elif re.search(r"极差|range", problem, re.IGNORECASE):
            want = "range"
        if not want:
            return None
        # 数据列表形态：[1, 2, 3, 4, 5] / {1,2,3} / 1, 2, 3, 4
        m = re.search(r"[\[{（(]\s*(-?\d+(?:\.\d+)?(?:\s*[,，;；]\s*-?\d+(?:\.\d+)?)+)\s*[\]}）)]", problem)
        if not m:
            m = re.search(r"(?:列表|数据)[：:为]?\s*(-?\d+(?:\.\d+)?(?:\s*[,，]\s*-?\d+(?:\.\d+)?){2,})", problem)
        if not m:
            return None
        try:
            nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", m.group(1))]
        except Exception:
            return None
        if len(nums) < 2:
            return None
        n = len(nums)
        if want == "mean":
            return f"平均值 = {self._format(self.sympy.Rational(sum(nums), n))}"
        if want == "median":
            s = sorted(nums)
            med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
            return f"中位数 = {med}"
        if want == "variance":
            mu = sum(nums) / n
            var = sum((x - mu) ** 2 for x in nums) / n
            return f"方差 = {var}"
        if want == "std":
            mu = sum(nums) / n
            var = sum((x - mu) ** 2 for x in nums) / n
            return f"标准差 = {self.sympy.sqrt(var)}"
        if want == "range":
            return f"极差 = {max(nums) - min(nums)}"
        return None

    # 4) 线性回归：最小二乘斜率/截距（给定数据点）
    def _linear_regression(self, problem: str) -> Optional[str]:
        if not re.search(r"回归|regression|least squares|最小二乘", problem, re.IGNORECASE):
            return None
        # 数据点形态：(1,2),(2,3),(3,5) 或 (1,2) (2,3) (3,5)
        points = re.findall(r"[（(]\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*[)）]", problem)
        if len(points) < 2:
            return None
        try:
            xs = [float(x) for x, _ in points]
            ys = [float(y) for _, y in points]
            n = len(xs)
            sx, sy = sum(xs), sum(ys)
            sxy = sum(x * y for x, y in zip(xs, ys))
            sxx = sum(x * x for x in xs)
            denom = n * sxx - sx * sx
            if abs(denom) < 1e-12:
                return None
            slope = (n * sxy - sx * sy) / denom
            intercept = (sy - slope * sx) / n
            want_slope = re.search(r"斜率|slope", problem, re.IGNORECASE)
            want_int = re.search(r"截距|intercept", problem, re.IGNORECASE)
            if want_slope and not want_int:
                return f"回归斜率 = {self._format(self.sympy.nsimplify(slope))}"
            if want_int and not want_slope:
                return f"回归截距 = {self._format(self.sympy.nsimplify(intercept))}"
            return (f"回归方程 y = {self._format(self.sympy.nsimplify(slope))}x "
                    f"{'+' if intercept >= 0 else '-'} {self._format(self.sympy.nsimplify(abs(intercept)))}")
        except Exception:
            return None

    # 5) 矩阵目标运算：行列式/逆/秩/迹/转置
    def _matrix_target(self, problem: str) -> Optional[str]:
        if not re.search(r"矩阵|matrix", problem, re.IGNORECASE):
            return None
        want = None
        if re.search(r"行列式|determinant|det\b", problem, re.IGNORECASE):
            want = "det"
        elif re.search(r"逆矩阵|inverse", problem, re.IGNORECASE):
            want = "inv"
        elif re.search(r"秩|rank", problem, re.IGNORECASE):
            want = "rank"
        elif re.search(r"迹|trace", problem, re.IGNORECASE):
            want = "trace"
        elif re.search(r"转置|transpose", problem, re.IGNORECASE):
            want = "transpose"
        if not want:
            return None
        parts = self._math_parts(problem)
        # V2.5 扩展：Python 列表格式 [[1,2],[3,4]] 直接从题面提取（不经 math_parts）
        py_rows = None
        mpy = re.search(r"\[\s*\[[-\d\s,.]+\]\s*(?:,\s*\[[-\d\s,.]+\]\s*)+\]", problem)
        if mpy:
            try:
                py_rows = [
                    [self._latex_to_sympy(cell) for cell in row_text.split(",")]
                    for row_text in re.findall(r"\[([-\d\s,.]+)\]", mpy.group(0))
                ]
            except Exception:
                py_rows = None
        for part in parts:
            m = re.search(r"\\begin\{[pb]?matrix\}(.+?)\\end\{[pb]?matrix\}", part, re.DOTALL)
            rows = None
            if m:
                try:
                    rows = [
                        [self._latex_to_sympy(cell) for cell in row.split("&")]
                        for row in re.split(r"\\\\", m.group(1))
                    ]
                except Exception:
                    rows = None
            if rows is None and py_rows is not None:
                rows = py_rows
            if not rows or any(len(r) != len(rows) for r in rows):
                continue
            try:
                mat = self.sympy.Matrix(rows)
                if want == "det":
                    return f"行列式 = {self._format(mat.det())}"
                if want == "inv":
                    return f"逆矩阵 = {self._format(mat.inv())}"
                if want == "rank":
                    return f"秩 = {mat.rank()}"
                if want == "trace":
                    return f"迹 = {self._format(mat.trace())}"
                if want == "transpose":
                    return f"转置 = {self._format(mat.T)}"
            except Exception:
                continue
        return None

    # 6) 复分析：|z| / 共轭 / 实部 / 虚部
    def _complex_ops(self, problem: str) -> Optional[str]:
        want = None
        if re.search(r"模长|模\b|magnitude|absolute value", problem, re.IGNORECASE):
            want = "abs"
        elif re.search(r"共轭|conjugate", problem, re.IGNORECASE):
            want = "conj"
        elif re.search(r"实部|real part", problem, re.IGNORECASE):
            want = "real"
        elif re.search(r"虚部|imaginary part", problem, re.IGNORECASE):
            want = "imag"
        if not want:
            return None
        m = re.search(r"([-+]?\d+(?:\.\d+)?\s*[-+]\s*\d+(?:\.\d+)?[ij])", problem, re.IGNORECASE)
        if not m:
            return None
        text = m.group(1).replace("i", "I").replace("j", "I")
        try:
            expr = self._parse(text)
            if want == "abs":
                return f"|{m.group(1)}| = {self._format(self.sympy.Abs(expr))}"
            if want == "conj":
                return f"共轭 = {self._format(self.sympy.conjugate(expr))}"
            if want == "real":
                return f"实部 = {self._format(self.sympy.re(expr))}"
            if want == "imag":
                return f"虚部 = {self._format(self.sympy.im(expr))}"
        except Exception:
            return None
        return None

    # 7) 向量运算：点积/叉积/模长/夹角
    def _vector_ops(self, problem: str) -> Optional[str]:
        want = None
        if re.search(r"点积|内积|dot product|inner product", problem, re.IGNORECASE):
            want = "dot"
        elif re.search(r"叉积|外积|cross product", problem, re.IGNORECASE):
            want = "cross"
        elif re.search(r"夹角|angle between", problem, re.IGNORECASE):
            want = "angle"
        elif re.search(r"模长|范数|长度|norm|magnitude", problem, re.IGNORECASE):
            want = "norm"
        if not want:
            return None
        vecs = re.findall(r"[（(]\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)(?:\s*[,，]\s*(-?\d+(?:\.\d+)?))?\s*[)）]", problem)
        # 单向量模长：只有一个向量
        if want == "norm" and not vecs:
            vecs = re.findall(r"向量\s*[（(]\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)(?:\s*[,，]\s*(-?\d+(?:\.\d+)?))?\s*[)）]", problem)
        if not vecs or (want != "norm" and len(vecs) < 2):
            return None
        try:
            def _v(t):
                return [float(x) for x in t if x not in (None, "")]
            v1, v2 = _v(vecs[0]), _v(vecs[1]) if len(vecs) > 1 else None
            if want == "norm":
                n1 = sum(a * a for a in v1) ** 0.5
                return f"模长 = {self._format(self.sympy.nsimplify(n1))}"
            if want == "dot":
                d = sum(a * b for a, b in zip(v1, v2))
                return f"点积 = {self._format(self.sympy.nsimplify(d))}"
            if want == "cross":
                if len(v1) != 3:
                    return None
                import itertools
                c = [
                    v1[1] * v2[2] - v1[2] * v2[1],
                    v1[2] * v2[0] - v1[0] * v2[2],
                    v1[0] * v2[1] - v1[1] * v2[0],
                ]
                return f"叉积 = ({c[0]}, {c[1]}, {c[2]})"
            if want == "angle":
                d = sum(a * b for a, b in zip(v1, v2))
                n1 = sum(a * a for a in v1) ** 0.5
                n2 = sum(b * b for b in v2) ** 0.5
                if n1 == 0 or n2 == 0:
                    return None
                cos_theta = d / (n1 * n2)
                if cos_theta < -1 or cos_theta > 1:
                    return None
                import math
                deg = math.degrees(math.acos(cos_theta))
                return f"夹角 = {deg:.2f}°"
        except Exception:
            return None
        return None

    # 8) 等差/等比数列求和
    def _series_sum(self, problem: str) -> Optional[str]:
        if not re.search(r"数列求和|前\s*\d+\s*项和|sum of the first", problem, re.IGNORECASE):
            return None
        # 等差数列：首项 a、公差 d、项数 n
        m = re.search(r"首项\s*[=为]?\s*(-?\d+)", problem) \
            or re.search(r"first\s+term\s*[=:]?\s*(-?\d+)", problem, re.IGNORECASE)
        n = re.search(r"前\s*(\d+)\s*项|first\s+(\d+)\s*terms?", problem, re.IGNORECASE)
        d = re.search(r"公差\s*[=为]?\s*(-?\d+)", problem) \
            or re.search(r"common\s+difference\s*[=:]?\s*(-?\d+)", problem, re.IGNORECASE)
        if not (m and n and d):
            return None
        try:
            a1, diff, count = int(m.group(1)), int(d.group(1)), int(n.group(1) or n.group(2))
            total = count * (2 * a1 + (count - 1) * diff) // 2
            return f"前 {count} 项和 = {total}"
        except Exception:
            return None

    # 9) 一阶 ODE：y' = f(x) 或 y' + p y = q 型显式解
    def _ode_first_order(self, problem: str) -> Optional[str]:
        if not re.search(r"微分方程|ode|differential equation", problem, re.IGNORECASE):
            return None
        if not re.search(r"求.{0,20}(?:解|通解|y\b)", problem) \
                and not re.search(r"solve", problem, re.IGNORECASE):
            return None
        # 撇号兼容：ASCII ' 与 Unicode ′ (U+2032)
        _prime = "['\u2032]"
        # 形态1: y' = f(x)（最简单：直接积分）。用排除法取表达式
        # （不含中文/逗号/分号），避免惰性匹配在空格+中文处无法收敛。
        m = re.search(
            r"y" + _prime + r"?\s*=\s*([0-9A-Za-z_+\-*/^().\s]+?)(?=[,，。；;]|[\u4e00-\u9fff]|$)",
            problem)
        if m:
            expr_text = self._latex_to_sympy(m.group(1).strip())
            result = self._integral(expr_text)
            if result is not None and "Integral" not in result:
                return f"通解 y = {result} + C"
        # 形态2: y' + p(x) y = q(x)（一阶线性）。q 用排除法取到中文/标点前。
        m2 = re.search(
            r"y" + _prime + r"?\s*\+\s*(\d+)\s*y\s*=\s*([0-9A-Za-z_+\-*/^().\s]+?)(?=[,，。；;]|[\u4e00-\u9fff]|$)",
            problem)
        if m2:
            p, q = m2.group(1), m2.group(2)
            expr_q = self._parse(self._latex_to_sympy(q.strip()))
            x = self.sympy.Symbol("x")
            try:
                import sympy as sp
                pp = sp.Integer(int(p))
                if pp != 0:
                    sol = sp.exp(-pp * x) * sp.integrate(sp.exp(pp * x) * expr_q, x)
                    return f"通解 y = {self._format(sol)} + C e^{{-{p}x}}"
            except Exception:
                return None
        return None

    def _run(self, operation) -> Optional[str]:
        if not self.sympy:
            return None
        try:
            return self._format(operation(self.sympy))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 整数确定性计算（同余 / 模幂，最可靠）
    # ------------------------------------------------------------------
    @staticmethod
    def _congruence(problem: str) -> Optional[str]:
        normalized = (problem.replace(r"\equiv", "≡").replace(r"\pmod{", " mod ")
                      .replace(r"\pmod", " mod ").replace("}", ""))
        match = re.search(r"(-?\d+)\s*\*?\s*x\s*≡\s*(-?\d+)\s*(?:mod\s*|\b)(\d+)", normalized)
        if not match:
            return None
        coefficient, constant, modulus = map(int, match.groups())
        divisor = gcd(coefficient, modulus)
        if constant % divisor:
            return "无解"
        if divisor != 1:
            return None  # 多解情形交给 LLM
        solution = (pow(coefficient % modulus, -1, modulus) * constant) % modulus
        return f"x={solution} (mod {modulus})"

    @staticmethod
    def _modular_power(problem: str) -> Optional[str]:
        normalized = problem.replace(r"\bmod", "mod").replace("{", "").replace("}", "")
        match = re.search(r"(-?\d+)\s*\^\s*(\d+)\s*mod\s*(\d+)", normalized)
        if not match:
            return None
        base, exponent, modulus = map(int, match.groups())
        if modulus == 0:
            return None
        return str(pow(base, exponent, modulus))

    # ------------------------------------------------------------------
    # 解析辅助
    # ------------------------------------------------------------------
    def _parse(self, expression: str):
        from sympy.parsing.sympy_parser import (
            implicit_multiplication_application, parse_expr, standard_transformations)

        if not re.fullmatch(r"[0-9A-Za-z_+\-*/^().,\s]+", expression):
            raise ValueError("unsupported symbolic expression")
        identifiers = set(re.findall(r"[A-Za-z]+", expression))
        allowed = {"sin", "cos", "tan", "asin", "acos", "atan", "log", "exp", "sqrt", "pi", "oo"}
        if any(identifier not in allowed and len(identifier) != 1 for identifier in identifiers):
            raise ValueError("unsupported symbolic identifier")
        return parse_expr(expression,
                          transformations=standard_transformations
                          + (implicit_multiplication_application,))

    def _math_parts(self, problem: str) -> list[str]:
        parts = re.findall(r"\$([^$]+)\$", problem)
        parts.extend(self._raw_latex_parts(problem))
        parts.extend(self._plain_equations(problem))
        # V2.5 扩展：裸 Unicode 积分（∫）与 Python 列表矩阵（[[...]]）不经 $ 包裹
        if not parts and "∫" in problem:
            parts.append(problem)
        if not parts and re.search(r"\[\s*\[[-\d\s,.]+\]\s*(?:,\s*\[[-\d\s,.]+\]\s*)+\]", problem):
            parts.append(problem)
        return parts

    @staticmethod
    def _raw_latex_parts(problem: str) -> list[str]:
        return re.findall(r"(\\(?:lim|int)[^。？?\n]+)", problem)

    @staticmethod
    def _plain_equations(problem: str) -> list[str]:
        if "$" in problem or not re.search(r"方程|求解|equation|solve|roots?|zeros?", problem, re.IGNORECASE):
            return []
        return re.findall(
            r"([0-9xyzXYZ(][0-9A-Za-z_+\-*/^().,\s]{0,120}=[0-9A-Za-z_+\-*/^().,\s]{1,120})", problem)

    @staticmethod
    def _latex_to_sympy(expression: str) -> str:
        value = expression.strip().replace("$", "")
        value = SympyPreSolver._replace_fractions(value)
        value = re.sub(r"\\sqrt\{([^{}]+)\}", r"sqrt(\1)", value)
        function_names = {"arctan": "atan", "arcsin": "asin", "arccos": "acos", "sin": "sin",
                          "cos": "cos", "tan": "tan", "log": "log", "ln": "log", "exp": "exp"}
        value = re.sub(r"\\(arctan|arcsin|arccos|sin|cos|tan|log|ln|exp)",
                       lambda m: f" {function_names[m.group(1)]}", value)
        value = (value.replace(r"\left", "").replace(r"\right", "").replace(r"\!", "")
                 .replace(r"\pi", "pi").replace(r"\infty", "oo").replace(r"\,", ""))
        value = value.replace("^", "**").replace("{", "(").replace("}", ")")
        value = re.sub(r"(?<=[xyzXYZ])(?=[xyzXYZ])", "*", value)
        return re.sub(r"(?<![A-Za-z])e(?=\s*\*\*)", "E", value)

    @staticmethod
    def _replace_fractions(value: str) -> str:
        marker = r"\frac"
        while marker in value:
            start = value.find(marker)
            numerator = SympyPreSolver._braced_group(value, start + len(marker))
            if numerator is None:
                break
            numerator_text, after_numerator = numerator
            denominator = SympyPreSolver._braced_group(value, after_numerator)
            if denominator is None:
                break
            denominator_text, after_denominator = denominator
            value = value[:start] + f"({numerator_text})/({denominator_text})" + value[after_denominator:]
        return value

    @staticmethod
    def _braced_group(value: str, start: int) -> Optional[tuple]:
        while start < len(value) and value[start].isspace():
            start += 1
        if start >= len(value) or value[start] != "{":
            return None
        depth = 0
        for index in range(start, len(value)):
            if value[index] == "{":
                depth += 1
            elif value[index] == "}":
                depth -= 1
                if depth == 0:
                    return value[start + 1:index], index + 1
        return None

    @staticmethod
    def _format(value: Any) -> str:
        text = str(value).replace("**", "^")
        text = re.sub(r"\blog\(", "ln(", text)
        text = re.sub(r"\batan\(", "arctan(", text)
        text = re.sub(r"\basin\(", "arcsin(", text)
        text = re.sub(r"\bacos\(", "arccos(", text)
        text = re.sub(r"\bexp\(x\)", "e^x", text)
        text = re.sub(r"\bexp\(([^()]+)\)", r"e^(\1)", text)
        return re.sub(r"(?<![A-Za-z])oo(?![A-Za-z])", "∞", text)
