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

        # 积分
        if re.search(r"积分|\\int|integral|integrate", problem, re.IGNORECASE):
            for part in math_parts:
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
