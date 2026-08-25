"""V4.5 出厂前跑题拦截（set1 round4 实证 #16：官方答案 2017+1)/3，
出厂却是一个 65 位大整数——模型串题答了另一道题）。

策略（保守，宁可漏杀不可误伤）：
- 只对 computation 非客观题启用；
- 数字答案：答案中的数字串与题面数字串无 ≥3 位公共子串 → 疑似串题；
- 文本答案：英文实体词交集（len>=4 的英文词，排除停用词）为空且陌生词
  ≥3 → 疑似串题（复用 cross_validator._reasoning_off_topic 的判据）；
- 任一条件不满足（答案太短/题面无数字/无法提取）→ 返回 False（不拦截）。
- 触发后由调用方路由重推（reconciliation），不直接丢弃答案。
"""
from __future__ import annotations

import re

#: 与 _reasoning_off_topic 相同的停用词表
_STOPWORDS = {
    "therefore", "hence", "thus", "answer", "result", "final", "then", "when",
    "where", "which", "with", "that", "this", "from", "into", "over", "under",
    "must", "will", "would", "should", "have", "has", "been", "being", "also",
    "each", "every", "some", "such", "there", "these", "those", "first",
}

_DIGIT_RE = re.compile(r"\d[\d,，]*\d|\d")


def _digits(text: str) -> list[str]:
    """提取文本中的数字串（去掉逗号/空格）。"""
    out = []
    for m in _DIGIT_RE.finditer(text or ""):
        d = re.sub(r"[,，\s]", "", m.group(0))
        if d.isdigit() and len(d) >= 1:
            out.append(d)
    return out


def _common_substr(a: str, b: str, min_len: int = 3) -> bool:
    """两数字串是否有 ≥min_len 位公共子串。"""
    if len(a) < min_len or len(b) < min_len:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    for i in range(len(shorter) - min_len + 1):
        if shorter[i:i + min_len] in longer:
            return True
    return False


def answer_off_topic(problem: str, answer: str) -> bool:
    """出厂前跑题检查。True = 疑似串题（调用方应路由重推）。"""
    p = str(problem or "").strip()
    a = str(answer or "").strip()
    if not p or not a:
        return False
    # 答案头部太短（如"2"、"3/4"）无法可靠判定 → 不拦截
    if len(a) < 8:
        return False
    # 通道 1：数字答案。答案含≥8 位数字串且**无英文解释**（纯数字答案）
    # 且与题面数字无公共子串 → 串题。
    a_digits = _digits(a)
    if any(len(d) >= 8 for d in a_digits):
        # 带英文实体词（≥3 个）的答案视为有解释的正文，不判跑题
        # （正确的大数答案如 "lattice points is 12679645, computed by ..."
        # 与题面数字 2009 本无公共子串，不能误伤）。
        a_words = set(re.findall(r"[A-Za-z]{4,}", a)) - _STOPWORDS
        if len(a_words) >= 3:
            return False
        p_digits = _digits(p)
        if p_digits:
            big = [d for d in a_digits if len(d) >= 8]
            if all(not any(_common_substr(d, pd) for pd in p_digits) for d in big):
                return True
        return False  # 题面无数字，无法判定
    # 通道 2：文本答案。英文实体词交集。
    a_words = set(re.findall(r"[A-Za-z]{4,}", a)) - _STOPWORDS
    p_words = set(re.findall(r"[A-Za-z]{4,}", p))
    if len(a_words) < 3:
        return False
    unknown = a_words - p_words
    return len(unknown) >= 3 and len(unknown) >= 0.6 * len(a_words)
