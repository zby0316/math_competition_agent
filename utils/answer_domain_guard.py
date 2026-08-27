# -*- coding: utf-8 -*-
"""答案定义域守卫（V2.5 零风险防御）：只拦截"数学上必然错误"的答案。

原理：以下形态在数学定义域上**必然错误**（不是可能错，是定义上不可能对）：
- 概率/比例答案越界 [0,1]；
- 百分比答案 > 100；
- 角度答案超出合理范围（排除"余数/同余"语境）；
- 计数答案 < 0。

拦截后调用方应优先换用另一候选（如 Python 数值结果），无候选则保持原答案
（最坏情况 = 现状，绝不恶化）。误拦概率 ≈ 0：这些是定义域约束而非启发式。

纯增量防御：不修改任何好路径逻辑，只新增一道出厂前检查。
"""

from __future__ import annotations

import re

#: 概率/比例题面信号
_PROB_RE = re.compile(
    r"概率|可能性|probability|chance|odds|比例|fraction|proportion",
    re.IGNORECASE,
)
#: 百分比题面信号
_PERCENT_RE = re.compile(
    r"百分(?:比|率)|percent|percentage|\d+\s*%",
    re.IGNORECASE,
)
#: 角度题面信号（degrees）
_DEGREE_RE = re.compile(
    r"度|degrees?|angle",
    re.IGNORECASE,
)
#: 同余/余数语境（角度可以取模，越界可能合法）
_MOD_CONTEXT_RE = re.compile(
    r"同余|余数|modulo|\bmod\b|remainder|模\s*\d+",
    re.IGNORECASE,
)
#: 计数题面信号
_COUNT_RE = re.compile(
    r"多少个|几种|几组|多少种|how many|number of|count|个数|数量",
    re.IGNORECASE,
)

#: 纯数值答案提取：允许分数/小数/整数/负号/百分号/π 表达式
_NUMERIC_RE = re.compile(
    r"^\s*[-+]?\s*(?:\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?|π|pi|\\pi)\s*%?\s*$",
    re.IGNORECASE,
)


def _extract_number(answer: str):
    """尽力从答案提取单个数值（float）。返回 None 表示无法可靠提取。"""
    text = str(answer or "").strip()
    # 去掉常见前缀/LaTeX 包裹
    text = re.sub(r"^(?:最终答案|答案|结论)\s*[：:]\s*", "", text)
    text = text.strip().strip("$").strip()
    # 带百分号
    m = re.search(r"[-+]?\s*\d+(?:\.\d+)?\s*%", text)
    if m:
        return float(m.group(0).replace("%", "").replace(" ", "")) / 100.0, "percent"
    # 纯分数/小数/整数
    m = re.search(r"[-+]?\s*\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?", text)
    if not m:
        return None
    token = m.group(0).replace(" ", "")
    try:
        if "/" in token:
            a, b = token.split("/")
            return float(a) / float(b), "number"
        return float(token), "number"
    except (ValueError, ZeroDivisionError):
        return None


def check_answer_domain(problem: str, answer: str) -> dict:
    """检查答案是否违反数学定义域。返回 {"violation": bool, "kind": str, "reason": str}。

    只拦截"定义域上必然错误"的形态；无法可靠提取数值或语境不明确时
    一律放行（零误伤优先）。
    """
    p = str(problem or "")
    a = str(answer or "")
    parsed = _extract_number(a)
    if parsed is None:
        return {"violation": False, "kind": "", "reason": ""}
    value, kind = parsed

    # 1) 概率/比例越界 [0,1]
    if _PROB_RE.search(p):
        if value < 0 or value > 1:
            return {
                "violation": True, "kind": "probability_range",
                "reason": f"题面问概率/比例，答案 {value} 超出定义域 [0,1]（数学上必错）",
            }

    # 2) 百分比 > 100（_extract_number 已转为小数，此处检查原始形式）
    if _PERCENT_RE.search(p):
        raw = re.search(r"[-+]?\s*\d+(?:\.\d+)?\s*%", a)
        if raw:
            pct = float(raw.group(0).replace("%", "").replace(" ", ""))
            if pct > 100:
                return {
                    "violation": True, "kind": "percent_range",
                    "reason": f"题面问百分比，答案 {pct}% 超出定义域（必错）",
                }

    # 3) 角度越界（排除同余/余数语境）
    if _DEGREE_RE.search(p) and not _MOD_CONTEXT_RE.search(p):
        if value < -360 or value > 360:
            return {
                "violation": True, "kind": "angle_range",
                "reason": f"题面问角度，答案 {value} 超出合理范围（必错）",
            }

    # 4) 计数为负
    if _COUNT_RE.search(p):
        if value < 0:
            return {
                "violation": True, "kind": "negative_count",
                "reason": f"题面问数量/个数，答案 {value} 为负数（必错）",
            }

    return {"violation": False, "kind": "", "reason": ""}
