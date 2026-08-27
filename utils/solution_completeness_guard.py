# -*- coding: utf-8 -*-
"""解集完整性守卫（V2.5，easy112 错题归因 P1：漏解类 3/20 题）。

实证根因（easy112 idx=11/76/111）：
- idx=111：sin x + cos x = √2 只答 π/4，漏周期解 5π/4 —— "Find all" 漏解；
- idx=11：鸽巢最小保证数 N=c(k-1)+1 边界差 1（7 vs 8）；
- idx=76：Wason 卡片最少翻牌漏翻一张（2 vs 3）。

这些题共同特征：**答案是一个"集合/数量"而非单个值**，且小规模可机器
验证。本守卫做两层：
1. detect：检出"Find all / 所有解 / 全部值 / 最少/最小保证"类题；
2. prompt_clause：注入强制条款——LLM 解出后必须用代码枚举全部解并
   打印解数（"共 N 个解：..."），与候选答案的解数/内容逐项核对。

与 counting_guard 的区别：counting_guard 防"计数题只写闭式"；
本守卫防"多解/边界题漏解"——重点在**解的数量与覆盖**。
"""

from __future__ import annotations

import re

#: "所有解/全部值/Find all"类题信号（要求完整解集）
_ALL_SOLUTIONS_RE = re.compile(
    r"find\s+all|all\s+values|all\s+solutions|all\s+possible|"
    r"所有\s*[解值根]|全部\s*[解值根]|所有的?[解值根]|"
    r"所有.{0,3}[解值根]|全部.{0,3}[解值根]|"
    r"所有可能|全部可能|求\s*所有|列出\s*所有|"
    r"的所有[解值根]|的全部[解值根]|"
    r"所有正整数解|所有整数解|所有实数解|所有复根|所有实根|所有根",
    re.IGNORECASE,
)

#: 最小保证/最少数量类（边界差 1 高危）
_MIN_GUARANTEE_RE = re.compile(
    r"最少|最小保证|guarantee|至少.{0,8}(?:保证|相同|同色)|"
    r"minimum\s+number|least\s+number|smallest\s+number|"
    r"必须.{0,6}(?:买|翻|移|拿|取)|need\s+to\s+(?:buy|flip|move|take)",
    re.IGNORECASE,
)

#: 解数/枚举代码证据
_ENUM_CODE_RE = re.compile(
    r"\bfor\s+\w+\s+in\b|\brange\s*\(|itertools|combinations\s*\(|"
    r"permutations\s*\(|product\s*\(|solveset\s*\(|nsolve\s*\(|"
    r"len\s*\(|roots\s*\(|solve\s*\(",
    re.IGNORECASE,
)


def detect_solution_completeness(problem: str) -> bool:
    """是否"完整解集/边界保证"类题。"""
    text = str(problem or "")
    return bool(_ALL_SOLUTIONS_RE.search(text)) or bool(_MIN_GUARANTEE_RE.search(text))


def prompt_clause(problem: str) -> str:
    """命中时注入解集完整性强制条款。"""
    if not detect_solution_completeness(problem):
        return ""
    return (
        "\n【解集完整性强制条款】本题要求完整解集/最少保证数，漏解或少算 1 即错。"
        "必须："
        "(1) 解出全部候选后用代码（sympy solveset/solve 或枚举）求出完整解集，"
        "并打印解的数量：print(\"解数:\", len(解集))；"
        "(2) 若题目给范围（如 0≤x<2π），在范围内完整枚举，不得漏周期解/边界解；"
        "(3) 最小保证类题（鸽巢原理等）先验证边界情形：k-1 个不行、k 个一定行，"
        "确认答案不是差 1 的值；"
        "(4) 最终答案必须与代码枚举的解集逐项一致。"
    )


def code_has_solution_enumeration(code: str) -> bool:
    """静态核查：代码是否含解集枚举/求解证据。"""
    code = str(code or "")
    if not code.strip():
        return False
    if _ENUM_CODE_RE.search(code):
        return True
    return False
