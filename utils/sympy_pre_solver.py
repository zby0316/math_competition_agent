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
        # V2.6.1 多条件守卫：sympy 各操作只解首个无歧义目标，无法应用题面的
        # 不等式/附加约束（"求满足 x²=4 且 x>0 的 x"会被解成两个根），
        # 此类题绝不短路 LLM（宁可不省时间，也不给确定性错误答案）。
        # V2.7.2 审计补强：单字约束词（正根/负根/实根/整数解/非负解等）同样
        # 无法机械应用——"x²=4 的正根"若短路会把两个根全交出去并被
        # sympy_deterministic_override 以 confidence=1.0 锁定且跳过 python
        # 反证，无法被下游纠正。凡含约束词一律交回 LLM。
        if re.search(
                r"且|并且|同时满足|正根|负根|实根|整数解|正整数解|非负解|非正解|"
                r"正整数|非负整数|大于\s*0|小于\s*0|大于\s*零|小于\s*零|"
                r"正数解|负数解|>0|<0|≥0|≤0",
                str(problem or "")):
            return {"hints": [], "whole_answer": ""}
        # V2.6.2 近似算法守卫：题面要求数值近似方法（梯形/辛普森/数值积分）时，
        # sympy 会算出**精确值**而非题面要求的近似值（真机实证："用梯形公式
        # 二等分计算 ∫₀¹x²dx"被算出精确的 1/3，而题面答案是 3/8）——绝不短路。
        if re.search(r"梯形|辛普森|Simpson|数值积分|近似(?:计算|值)?", str(problem or "")):
            return {"hints": [], "whole_answer": ""}
        try:
            hints = self.hints_for(str(problem or ""))
        except Exception:  # noqa: BLE001 - 确定性工具失败交回 LLM
            return {"hints": [], "whole_answer": ""}
        whole_answer = ""
        for hint in hints:
            if hint.get("whole_goal") and hint.get("result"):
                op = hint.get("operation", "")
                result_text = str(hint["result"])
                # V2.6.2 未定值/泄漏守卫：定积分与极限的结果必须是自包含数值
                # （不得含自由符号/未求值的特殊函数）——真机实证：∫₀¹ xⁿ/(1+x)dx
                # 被 sympy 算出含 n 的 lerchphi/gamma 未定值串，若短路会以垃圾
                # 出厂；宁可交回 LLM 也不给确定性错误答案。
                if op in {"definite_integral", "limit"} \
                        and not self._is_numeric_result(result_text):
                    continue
                if self._has_special_function_noise(result_text):
                    continue
                whole_answer = hint["result"]
                break
        if whole_answer:
            # V2.6.2 输出美化：显式乘号 * → ·（"3*x^2 + 2" → "3·x^2 + 2"），
            # 更接近教材写法，便于判分一眼识别（_format 已把 ** 归一为 ^，
            # 残留 * 均为乘积；matcher 的 _UNICODE_REPLACEMENTS 会把 · 归一
            # 回 *，交叉验证的符号比对不受影响）。
            whole_answer = whole_answer.replace("*", "·")
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

        # 算术求值（无积分/导数/极限/方程/同余/模幂等关键词时）。
        # V2.7 B1：容忍"的(值|结果)"尾缀（"计算 2+3 的值"）；组合数 C(n,k)
        # 由 _evaluate 翻译为 binomial。负向断言防抢同余/模幂/矩阵/积分题。
        arithmetic = re.search(
            r"(?:计算|求值|求|calculate|evaluate)\s*([0-9A-Za-z_+\-*/^().,\s×·]+?)\s*"
            r"(?:的(?:值|结果))?[。？?]?$",
            problem, re.IGNORECASE)
        if arithmetic and not re.search(
                r"积分|导数|极限|方程|余数|模|矩阵|integral|derivative|limit|equation|modulo|mod",
                problem, re.IGNORECASE):
            result = self._evaluate(arithmetic.group(1))
            if result is not None:
                # V2.6.1 完整性守卫：算术求值结果若仍含自由符号（如"求 x 的值"
                # 被算成 "x"、"求 3x" 被算成 "3*x"），不是完整数值答案，只能作为
                # 参考 hint，绝不能短路 LLM。
                hints.append({"operation": "evaluate", "result": result,
                              "whole_goal": self._is_numeric_result(result)})

        math_parts = self._math_parts(problem)

        # 积分（V2.7 B1：关键词补 Unicode 积分号 ∫）
        if re.search(r"积分|\\int|∫|integral|integrate", problem, re.IGNORECASE):
            for part in math_parts:
                definite = re.search(
                    r"(?:\\int|∫)\s*_?\{?([^}\s^]+)\}?\^?\{?([^}\s^]+)\}?\s*(.+?)(?:\\,|\s)*d([A-Za-z])\b", part)
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

        # 极限（V2.7 B1：关键词补裸 lim——注意 \blim 不带后边界，lim_ 里 _ 是 \w）
        if re.search(r"极限|\\lim|\blimit\b|\blim", problem, re.IGNORECASE):
            for part in math_parts:
                match = re.search(r"(?:\\lim|lim)\s*_?\{?\s*([A-Za-z])\s*(?:\\to|→)\s*([^}\s]+)\}?\s*(.+)", part)
                if match:
                    result = self._limit(self._latex_to_sympy(match.group(3)), match.group(1),
                                         self._latex_to_sympy(match.group(2)))
                    if result is not None:
                        hints.append({"operation": "limit", "result": result, "whole_goal": True})
                    break

        # 导数
        if re.search(r"导数|求导|微分|derivative|differentiate", problem, re.IGNORECASE):
            partial = re.search(
                r"f\s*\(\s*[A-Za-z]\s*,\s*[A-Za-z]\s*\)\s*=\s*(?P<expr>[^，。；;]+?)"
                r"\s*关于\s*\$?(?P<var>[A-Za-z])\$?\s*的?(?:偏导|导数)", problem)
            match = partial or re.search(
                r"(?:f\s*\(\s*(?P<var>[A-Za-z])\s*\)|y)\s*=\s*(?P<expr>[^，。；;]+?)"
                r"(?=\s*(?:的(?:导数|微分)|[,，。；;]|$))", problem)
            if match:
                variable = match.group("var") or "x"
                result = self._derivative(self._latex_to_sympy(match.group("expr")), variable)
                if result is not None:
                    hints.append({"operation": "derivative", "result": result, "whole_goal": True})
            else:
                # V2.7 B1：无 f(x)=/y= 前缀的"求 X 的导数"（X 须为纯 ASCII 数学式）
                match = re.search(
                    r"(?:求|对)\s*([0-9A-Za-z_+\-*/^().,\s]+?)\s*的?\s*(?:导数|导函数)",
                    problem)
                if match and re.fullmatch(r"[0-9A-Za-z_+\-*/^().,\s]+", match.group(1)):
                    result = self._derivative(self._latex_to_sympy(match.group(1)), "x")
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
        # V2.7 B1：组合数 C(n,k) → binomial(n,k)；Unicode 乘号 ×/· → *
        translated = re.sub(
            r"\bC\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)",
            r"binomial(\1,\2)", expression)
        translated = translated.replace("×", "*").replace("·", "*")
        return self._run(lambda _: self._parse(translated))

    def _is_numeric_result(self, result: str) -> bool:
        """结果是否不含自由符号（可独立作答的数值，V2.6.1 完整性守卫）。

        用 sympy 重解析判 free_symbols：把 "3*x"、"x" 这类符号残留挡在
        whole_goal 之外；"1/2"、"3.14"、"binomial(10,3)" 自动求值后为空集。
        """
        if not result or not self.sympy:
            return False
        try:
            return not self._parse(result).free_symbols
        except Exception:  # noqa: BLE001 - 判定失败按非数值处理，不短路
            return False

    @staticmethod
    def _has_special_function_noise(text: str) -> bool:
        """结果含未求值的特殊函数/原语 → 不能作为可判分答案短路（V2.6.2）。"""
        return bool(re.search(
            r"lerchphi|exp_polar|Piecewise|Integral\(|Limit\(|Sum\(|Product\(|"
            r"hyper\(|meijerg|gamma\(|zeta\(|\boo\b", str(text or "")))

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
        # 覆盖多种书写：3x≡7 mod 11 / 3x ≡ 7 (mod 11) / 3x≡7(模11) / 5x 同余 3 模 7
        # 括号修复：原正则对 "(mod 11)" 失败（\b 后跟字母不匹配 \d+）——V2.7 B1
        match = re.search(
            r"(-?\d+)\s*\*?\s*x\s*(?:≡|同余于?)\s*(-?\d+)\s*"
            r"(?:\(?\s*(?:mod|模)\s*\)?\s*|\b)(\d+)",
            normalized, re.IGNORECASE)
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
            # 中文自然语言形态（V2.7 B1）：
            #   "求 2^100 除以 13 的余数"（"除以"必须配"的余数"，否则是普通除法）
            #   "求 2^100 模 13 的余数/的值"（"模"直接表模幂）
            #   "求 3 的 10 次方除以 11 的余数" / "求 2 的 100 次幂模 13"
            match = re.search(
                r"求\s*(\d+)\s*(?:\^\s*(\d+)\s*|的\s*(\d+)\s*(?:次方|次幂|次乘方)\s*)"
                r"(?:除以\s*(\d+)\s*的\s*余数|模\s*(\d+)\s*(?:的\s*(?:余数|值))?)",
                problem, re.IGNORECASE)
        if not match:
            return None
        groups = [g for g in match.groups() if g is not None]
        base, modulus = int(groups[0]), int(groups[-1])
        # 指数在 groups[1]（^形态）或 groups[2]（次方形态）；取两者非空的
        exponent = int(next((g for g in groups[1:-1] if g is not None), "1"))
        if modulus == 0:
            return None
        return str(pow(base, exponent, modulus))

    # ------------------------------------------------------------------
    # 解析辅助
    # ------------------------------------------------------------------
    def _parse(self, expression: str):
        from sympy.parsing.sympy_parser import (
            convert_xor, implicit_multiplication_application, parse_expr,
            standard_transformations)

        if not re.fullmatch(r"[0-9A-Za-z_+\-*/^().,\s]+", expression):
            raise ValueError("unsupported symbolic expression")
        identifiers = set(re.findall(r"[A-Za-z]+", expression))
        allowed = {"sin", "cos", "tan", "asin", "acos", "atan", "log", "exp", "sqrt",
                   "pi", "oo", "binomial"}
        if any(identifier not in allowed and len(identifier) != 1 for identifier in identifiers):
            raise ValueError("unsupported symbolic identifier")
        # convert_xor：数学题里 ^ 必须是幂（parse_expr 默认对纯数字把 ^ 当 Python
        # XOR——"C(10,3)×3^7" 曾被算成 367=(360 XOR 7)，V2.7 B1 修复）
        return parse_expr(expression,
                          transformations=standard_transformations
                          + (convert_xor, implicit_multiplication_application))

    def _math_parts(self, problem: str) -> list[str]:
        parts = re.findall(r"\$([^$]+)\$", problem)
        parts.extend(self._raw_latex_parts(problem))
        parts.extend(self._plain_equations(problem))
        return parts

    @staticmethod
    def _raw_latex_parts(problem: str) -> list[str]:
        parts = re.findall(r"(\\(?:lim|int)[^。？?\n]+)", problem)
        # V2.7 B1：Unicode 积分号 ∫ 与裸 lim（"求 ∫_0^1 x^2 dx" / "求 lim_{x→0} ..."）
        parts.extend(re.findall(r"(∫[^。？?\n]+)", problem))
        parts.extend(re.findall(r"(\blim\s*_?\{?[^。？?\n]+)", problem))
        return parts

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
        # V2.7 B1：Unicode 乘号归一（"C(10,3)×3^7" 中文题）
        value = value.replace("×", "*").replace("·", "*")
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
