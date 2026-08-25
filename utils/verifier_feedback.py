"""验证失败反馈提取器（V5.0：IMO25 金牌管线的反例反馈机制）。

验证代码 FAIL 时，stdout 里往往有反例/错误细节（"n=2: candidate=7,
actual=5"、"counterexample found"、"不等成立" 等）。提取并结构化为
下一轮采样的约束提示——防止重复踩同一个坑（验证器反馈指导搜索，
比盲目多采样精准）。

合规：纯本地文本处理，无外部调用。
"""
from __future__ import annotations

import re

#: FAIL 相关的证据行模式（验证代码 print 的反例/错误细节）
_FAIL_LINE_RE = re.compile(
    r"(?i)(counterexample|反例|fail|失败|不等|错误|mismatch|不匹配|"
    r"expected|期望|actual|实际|≠|!=\s*|不成立|矛盾|contradiction)",
)

#: 数字/参数实例行（"n=2: ..."、"a=3,b=1: ..."）
_INSTANCE_RE = re.compile(r"(?:[a-zA-Z_]\s*=\s*\d+[\d\s,;:：]*){1,4}")

_MAX_LINES = 8
_MAX_CHARS = 500


def extract_feedback(stdout: str, stderr: str = "") -> str:
    """从验证代码输出中提取反例反馈（结构化文本）。

    返回形如：
    "验证器反馈：候选答案在以下实例验证失败：
     - n=2: candidate=7, expected=5（枚举得 5）
     - a=3,b=1: 不等式不成立"
    无可用反馈返回 ""。
    """
    text = str(stdout or "") + "\n" + str(stderr or "")
    if not text.strip():
        return ""
    # 逐行找 FAIL 证据行
    evidence = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        line = line.strip()
        if not line or len(line) > 200:
            continue
        if _FAIL_LINE_RE.search(line):
            # 只收含实质内容的行（数字/不等号/具体值）
            if re.search(r"\d|[≠!<>]|反例|expected|actual", line, re.I):
                evidence.append(line[:160])
        if len(evidence) >= _MAX_LINES:
            break
    if not evidence:
        return ""
    dedup = []
    for e in evidence:
        if e not in dedup:
            dedup.append(e)
    return "验证器反馈（上一轮候选验证失败，反例/错误如下，重解时必须避免重复错误）：\n" \
        + "\n".join("- " + e for e in dedup[:_MAX_LINES])[: _MAX_CHARS]


def extract_instance_feedback(stdout: str) -> str:
    """提取含具体参数实例的失败行（"n=2: ..."形态），供约束提示使用。"""
    text = str(stdout or "")
    if not text.strip():
        return ""
    hits = []
    for line in text.splitlines():
        line = line.strip()
        if _INSTANCE_RE.search(line) and _FAIL_LINE_RE.search(line) \
                and len(line) <= 200:
            hits.append(line[:160])
        if len(hits) >= 4:
            break
    if not hits:
        return ""
    return "；".join(hits)[:400]
