# -*- coding: utf-8 -*-
"""semantic_judge：确定性语义判定器（判分对齐层，V3.2）。

用途：
1. 本地判分交叉验证：LLM 判 wrong/error 时，用确定性规则复核，标记
   "判分疑似误杀"（答案其实与标准答案等价）——提高本地回归可信度。
2. 离线复盘：批量重判历史结果，量化格式/解析丢分。

judge(candidate, expected) -> True/False/None
判定通道（顺序）：
  0. 文本完全相等 → True
  1. expected 的 ≈ 之前符号部分与候选符号等价（latex→sympy→simplify→数值采样）
  2. ≈ 数值比较（相对容差 1e-4，小数值用自身量级）
  3. 数字集合匹配（句子型答案："元素 6 在 Z15 中的阶为 5" vs "5"）
  4. 完整 expected 分段符号等价
  5. 相似度兜底（>0.6 True；<0.35 且数字无交集 False；否则 None）

设计约束（评审共识）：
- 宁可 None（弃权）不误判：False 只在有明确证据时给出。
- 纯确定性，零 LLM 调用；复用于回归判分与离线分析。
"""
import re

from utils.answer_matcher import AnswerMatcher

_NUM_RE = re.compile(r"[-+]?\d+\.?\d*(?:[eE][-+]?\d+)?")
_APPROX_RE = re.compile(r"≈\s*([-+]?\d+\.?\d*(?:[eE][-+]?\d+)?)")
_TIMES10_RE = re.compile(r"([-+]?\d+\.?\d*(?:[eE][-+]?\d+)?)\s*[xX×]\s*10\^?\{?([-+]?\d+)\}?")
_SUP = str.maketrans({"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5",
                      "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9", "⁻": "-", "⁺": "+"})

_FINAL_PREFIXES = ("最终答案：", "最终答案:", "答案：", "答案:")


def ascii_math(s: str) -> str:
    """unicode 上标数字/负号 → ASCII（×10⁻⁴ → ×10-4）。"""
    return s.translate(_SUP)


def parse_approx(s: str):
    """从文本提取 ≈ 数值（含 ×10ⁿ 科学计数）。返回 float 或 None。"""
    s = ascii_math(s)
    for m in _TIMES10_RE.finditer(s):
        return float(m.group(1)) * 10.0 ** int(m.group(2))
    m = _APPROX_RE.search(s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def extract_numbers(s: str) -> list:
    out = []
    for m in _NUM_RE.finditer(s):
        try:
            out.append(float(m.group(0)))
        except ValueError:
            continue
    return out


def num_sets(s: str) -> set:
    return {round(n, 6) for n in extract_numbers(s)}


def norm_segments(s: str) -> list:
    """答案文本 → 表达式段：去 $、去结论前缀、按分号（中/英）/换行切、每段取 = 右侧。"""
    s = (s or "").replace("$$", "").replace("$", "")
    for pre in _FINAL_PREFIXES:
        if s.startswith(pre):
            s = s[len(pre):]
    segs = []
    for part in re.split(r"[;；]", s.replace("\n", ";")):
        part = part.strip().strip("。.,，,").strip()
        if not part:
            continue
        if "=" in part:
            part = part.split("=", 1)[1].strip()
        part = part.strip("。.,，,").strip()
        if part:
            segs.append(part)
    return segs


def final_answer_text(s: str) -> str:
    """从 final_response 提取纯答案部分（"最终答案："之后、首个章节标记之前）。"""
    s = str(s or "")
    for pre in _FINAL_PREFIXES:
        if pre in s:
            s = s.split(pre, 1)[1]
            break
    for cut in ("关键步骤", "验证", "解题过程", "步骤", "综上所述", "因此", "\n\n"):
        i = s.find(cut)
        if 0 < i < len(s):
            s = s[:i]
    return s.strip(" \n。.,，,；；")


def _sympy_eq(a, b):
    try:
        return AnswerMatcher._sympy_equivalent(a, b, 1e-6)
    except Exception:
        return None


def _numeric_judge(cand: str, approx: float):
    """候选 vs ≈ 数值：候选含近似值 → 相对容差 1e-4；含精确表达式 → sympy 数值化。"""
    cand_nums = [n for n in extract_numbers(cand) if abs(n) < 1e8]
    if not cand_nums:
        return None
    try:
        import sympy as sp
        expr_texts = norm_segments(cand)
        vals = []
        for t_ in expr_texts:
            # 段内再按 =/标点细分：中文叙述与表达式混排时（"其中 A=14(sinh1-1)/cosh1"）
            # 逐子段尝试数值化，任一成功即纳入候选值
            for sub in re.split(r"[=；;，,]", t_):
                sub = sub.strip()
                if not sub:
                    continue
                try:
                    e = sp.sympify(sub.replace("^", "**"), locals={})
                    vals.append(float(sp.N(e, 20)))
                except Exception:
                    continue
        if vals:
            cand_nums = vals
        elif re.search(r"[A-Za-z\\{}]|\\frac|\\dfrac", cand):
            # 无表达式可数值化且候选含符号/latex（如 u(r) 完整解）：杂散数字无判据，
            # 弃权而非误判 False；纯数字候选（"49/147456"）仍走杂散数字通道
            return None
    except Exception:
        # 候选含自由符号（如 u(r) 完整解）：杂散数字无判据，弃权而非误判 False
        return None
    for n in cand_nums:
        if abs(n - approx) <= max(1e-8, 1e-4 * abs(approx)):
            return True
    best = min(cand_nums, key=lambda n: abs(n - approx))
    if abs(best - approx) > 0.01 * max(1.0, abs(approx)):
        return False
    return None


def _digit_channel(cand: str, expected: str, approx):
    """数字集合通道：句子型答案（"元素 6 在 Z15 中的阶为 5" vs "5"）。"""
    cset = num_sets(cand)
    if not cset:
        return None
    if approx is not None:
        return None
    eset = num_sets(expected)
    if not eset:
        return None
    if eset <= cset:
        return True
    if eset & cset:
        return None
    return False


def judge(candidate, expected):
    """candidate/expected 文本 → True/False/None。"""
    cand = str(candidate or "").strip()
    exp = str(expected or "").strip()
    if not cand or not exp:
        return None
    if cand == exp:
        return True
    head, tail = (exp.split("≈", 1) + [""])[:2]
    head, tail = head.strip(), ("≈" + tail.strip()) if tail else ""
    approx = parse_approx(tail) if tail else None
    # 通道 1：expected ≈ 之前符号部分与候选符号等价
    if head:
        sc, se = norm_segments(cand), norm_segments(head)
        if sc and se and len(sc) == len(se):
            rs = [_sympy_eq(x, y) for x, y in zip(sc, se)]
            if all(r is True for r in rs):
                return True
            if any(r is False for r in rs) and approx is None:
                return False
        if len(se) > len(sc):
            cand_joined = " ; ".join(sc)
            for y in se:
                if _sympy_eq(cand_joined, y) is True:
                    return True
    # 通道 2：≈ 数值比较
    if approx is not None:
        r = _numeric_judge(cand, approx)
        if r is not None:
            return r
    # 通道 3：数字集合匹配（句子型答案）
    r = _digit_channel(cand, exp, approx)
    if r is not None:
        return r
    # 通道 4：完整 expected 分段符号等价
    sc, se = norm_segments(cand), norm_segments(exp)
    if sc and se and len(sc) == len(se):
        rs = [_sympy_eq(x, y) for x, y in zip(sc, se)]
        if all(r is True for r in rs):
            return True
        if any(r is False for r in rs):
            return False
    # 通道 5：相似度兜底（数字无交集才允许判 False）
    sim = AnswerMatcher._string_similarity(cand, exp)
    if sim > 0.6:
        return True
    if sim < 0.35 and not (num_sets(cand) & num_sets(exp)):
        return False
    return None


def grade_report(candidate, expected) -> dict:
    """带细节的判定报告（供 grade_llm 交叉验证与复盘）。"""
    return {
        "deterministic_verdict": judge(candidate, expected),
        "candidate": str(candidate or "")[:120],
        "expected": str(expected or "")[:120],
    }
