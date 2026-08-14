# -*- coding: utf-8 -*-
"""确定性数值比对：Python 验证答案 vs 推理候选答案。

背景（V2.4 全量评测 idx=85 实证）：Python 验证代码内部自洽（residue 与
独立公式一致 → PASS），但没有把**计算结果与 reasoning 候选**做数值比对——
留数符号写反的推理答案被标 support 出厂。本模块在验证证据产出后做一层
确定性兜底：只要 python answer 与候选都是可解析的数值/复数表达式，就
用 sympy 精确比对；不一致 → 把 evidence_status 强制降级为 contradict，
让下游 playoff/仲裁有机会用确定性计算纠正，而不是盲信"内部自洽"的 PASS。

口径：保守优先——解析不了、含自由符号、或两边是不同类对象时返回 None
（不降级），宁可漏判也不误伤。
"""
from __future__ import annotations

import re

import sympy as sp


def _read_braced(s: str, i: int):
    """从位置 i（指向 '{'）读取平衡花括号内容，返回 (content, 结束位置)。"""
    assert s[i] == "{"
    depth = 0
    j = i
    while j < len(s):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    return s[i + 1:], len(s)


def _read_token(s: str, i: int):
    """从位置 i 读取单个 token（不含空白/花括号），返回 (token, 结束位置)。"""
    j = i
    while j < len(s) and s[j] not in "{} \t\n":
        j += 1
    return s[i:j], j


def _latex_to_sympy(text: str) -> str:
    """把常见的 LaTeX 数学片段转成 sympy 可 parse 的形式（尽力而为）。

    支持：\\frac{a}{b}、\\sqrt{x}、\\pi、独立 i→I、\\cdot/\\times→*。
    使用平衡花括号展开，避免 \\sqrt{2} 的花括号干扰 \\frac 匹配。
    """
    t = str(text or "").strip()
    t = t.replace("\\cdot", "*").replace("\\times", "*")
    t = re.sub(r"\\pi", "pi", t)

    def _walk(s: str) -> str:
        out = []
        i = 0
        while i < len(s):
            c = s[i]
            if s.startswith("\\frac", i):
                i += 5
                while i < len(s) and s[i] == " ":
                    i += 1
                if i < len(s) and s[i] == "{":
                    a, i = _read_braced(s, i)
                else:
                    a, i = _read_token(s, i)
                while i < len(s) and s[i] == " ":
                    i += 1
                if i < len(s) and s[i] == "{":
                    b, i = _read_braced(s, i)
                else:
                    b, i = _read_token(s, i)
                out.append(f"({_walk(a)})/({_walk(b)})")
            elif s.startswith("\\sqrt", i):
                i += 5
                while i < len(s) and s[i] == " ":
                    i += 1
                if i < len(s) and s[i] == "{":
                    inner, i = _read_braced(s, i)
                else:
                    inner, i = _read_token(s, i)
                out.append(f"sqrt({_walk(inner)})")
            elif c == "i" and (i + 1 >= len(s) or not s[i + 1].isalnum()) \
                    and (i == 0 or not s[i - 1].isalnum()):
                # 独立虚数单位 i → I（\frac 里的分子 i 也覆盖）
                out.append("I")
                i += 1
            else:
                out.append(c)
                i += 1
        return "".join(out)

    out = _walk(t)
    # 虚数单位后紧跟 '(' 需要补乘号：I(...) 会被 sympy 当函数调用
    out = re.sub(r"I\(", "I*(", out)
    # 数字后紧跟字母/函数（如 4sqrt(2)、3pi）补乘号
    out = re.sub(r"(\d)([a-zA-Z(])", r"\1*\2", out)
    return out


def _extract_scalar_expression(text: str):
    """从答案字符串中提取一个可求值的 sympy 数值表达式。

    返回 (expr, ok)；ok=False 表示无法提取（保守返回 None）。
    只接受纯数值/复数/含 sqrt/pi/E/I 的表达式——含其他自由符号则拒绝。
    """
    if not text:
        return None, False
    t = str(text).strip()
    # 去掉 "变量=" 前缀：取最后一个 "=" 之后的内容（通常是化简后的最终式）
    if "=" in t:
        t = t.rsplit("=", 1)[1].strip()
    # 去掉尾部文本（如 "成立"、中文说明、$$ 包裹），只保留数学片段
    t = t.replace("$", "").replace("\\operatorname{Res}(f,e^{i\\pi/4})", "")
    # 多值答案（分号/顿号分隔）只取第一个含 = 或纯表达式片段
    t = re.split(r"[，。；;：]", t)[0].strip()
    # 尝试 LaTeX → sympy
    try:
        expr = sp.sympify(_latex_to_sympy(t))
    except Exception:  # noqa: BLE001
        # 兜底：提取第一个数值 token
        m = re.search(r"[-+]?\d+/\d+|[-+]?\d+\.?\d*", t)
        if not m:
            return None, False
        try:
            expr = sp.sympify(m.group(0))
        except Exception:  # noqa: BLE001
            return None, False
    if expr is None:
        return None, False
    free = {str(s) for s in expr.free_symbols}
    allowed = {"pi", "I", "E", "e", "oo"}  # 仅允许数值相关符号
    if free - allowed:
        return None, False
    return expr, True


def numeric_crosscheck(candidate_answer: str, python_answer: str):
    """确定性比对候选答案与 Python 验证答案的数值。

    返回 dict：
      {"status": "match"|"mismatch"|"inconclusive",
       "candidate_value": str|None, "python_value": str|None,
       "reason": str}
    status 为 "mismatch" 时调用方应将证据降级为 contradict。
    """
    cand_expr, cand_ok = _extract_scalar_expression(candidate_answer or "")
    py_expr, py_ok = _extract_scalar_expression(python_answer or "")
    if not (cand_ok and py_ok):
        return {"status": "inconclusive",
                "reason": "候选或 python 答案无法解析为数值表达式",
                "candidate_value": None, "python_value": None}
    # 复数/数值精确比对：sympy simplify(expr1 - expr2) == 0
    try:
        diff = sp.simplify(cand_expr - py_expr)
        if diff == 0:
            return {"status": "match",
                    "candidate_value": str(cand_expr),
                    "python_value": str(py_expr),
                    "reason": "数值精确相等"}
        # 符号形式不同但数值相等（如 -(-1)**(1/4)/4 vs (1+i)/(4√2) 的负号变体）
        # ——先做浮点交叉验证，避免符号化简盲区
        try:
            cv = complex(sp.N(cand_expr, 30))
            pv = complex(sp.N(py_expr, 30))
        except Exception:  # noqa: BLE001
            cv = pv = None
        if cv is not None and pv is not None and abs(cv - pv) < 1e-12:
            return {"status": "match",
                    "candidate_value": str(cand_expr),
                    "python_value": str(py_expr),
                    "reason": "数值相等（浮点交叉验证）"}
        return {"status": "mismatch",
                "candidate_value": str(cand_expr),
                "python_value": str(py_expr),
                "reason": f"数值不一致: 候选={str(cand_expr)} vs python={str(py_expr)}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "inconclusive", "reason": f"比对异常: {str(exc)[:80]}",
                "candidate_value": None, "python_value": None}
