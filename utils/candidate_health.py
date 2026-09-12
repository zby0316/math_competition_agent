# -*- coding: utf-8 -*-
"""候选健康度体检（移植 ICMA 99.11 candidate_health 的保守版，2026-09-12）。

四维体检：格式完整 / 数值健康 / 证据质量 / 反例自由。设计哲学：**宁可
unknown 不误杀**——只有"确定违反题面约束"的候选才判 fail（NaN/inf、
计数/概率/长度类题的负数答案、填空答案空位数不足）；contradict 证据只
标注不排除（冲突正是仲裁要裁决的内容）。零 LLM 成本，仅用于仲裁池准入
过滤，不参与答案改写。

保守性声明：全部规则都是"几乎必然错"的确定性硬伤，不存在把可能正确的
答案误杀的情形；任何不确定都回到 ok 状态交仲裁裁决。
"""

from __future__ import annotations

import re

from utils.question_profile import count_blanks, fill_part_count

#: 题面出现这些"非负语义"词时，候选答案中出现负数就是确定性硬伤。
_NON_NEGATIVE_CONTEXT_RE = re.compile(
    r"(?:个数|数量|数目|正整数|positive\s+integer|count|number\s+of|"
    r"概率|probability|长度|length|距离|distance|边长|side\s+length|"
    r"年龄|age|度数|degree|阶|order\s+of)",
    re.IGNORECASE)

#: 候选答案中出现的独立负值（排除指数/括号内系数等误报场景用后续守卫再收敛）。
_NEGATIVE_VALUE_RE = re.compile(r"(?<![\w\\])-\s*\d+(?:\.\d+)?")

_NAN_INF_RE = re.compile(r"\b(?:nan|inf(?:inity)?)\b", re.IGNORECASE)

_CONTRADICT_EVIDENCE_RE = re.compile(
    r"(?:矛盾|反例|contradiction|counterexample|violates|违反|不成立)",
    re.IGNORECASE)


def assess_candidate_health(
        answer: str,
        question_mode: str = "computation",
        problem: str = "",
        evidence: str = "",
) -> dict:
    """返回 {"status": ok|fail|contradict, "reasons": [...], "score": float}。

    status == "fail" 的候选应被排除出仲裁池（确定性硬伤）；
    "contradict" 只作标注，不排除。
    """
    text = (answer or "").strip()
    if not text:
        return {"status": "fail", "reasons": ["空答案"], "score": 0.0}

    reasons: list[str] = []
    status = "ok"

    # 1) NaN / inf —— 永不合法。
    if _NAN_INF_RE.search(text):
        reasons.append("nan_inf")
        status = "fail"

    # 2) 非负语义题面 + 候选出现独立负数 —— 确定性硬伤。
    #    守卫：只处罚"短纯数值"候选（长叙述答案里的负号常是推导残句），
    #    且负数必须带显式负号（"x-3" 这类表达式不处罚）。
    if status == "ok" and _NON_NEGATIVE_CONTEXT_RE.search(problem or ""):
        compact = re.sub(r"\s+", "", text)
        if len(compact) <= 80 and _NEGATIVE_VALUE_RE.search(compact) \
                and not re.search(r"[A-Za-z一-鿿]{2,}", compact):
            reasons.append("negative_value_in_nonnegative_context")
            status = "fail"

    # 3) 填空题空位数不足 —— 与判分器零容忍口径一致。
    if status == "ok" and question_mode == "fill":
        blanks = count_blanks(problem or "")
        if blanks > 0 and fill_part_count(text) < blanks:
            reasons.append("fill_blank_undercount")
            status = "fail"

    # 4) 证据矛盾标注（不排除，交仲裁）。
    if status == "ok" and evidence and _CONTRADICT_EVIDENCE_RE.search(evidence):
        status = "contradict"
        reasons.append("contradict_evidence")

    score = {"ok": 1.0, "contradict": 0.5, "fail": 0.0}[status]
    return {"status": status, "reasons": reasons, "score": score}
