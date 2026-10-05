"""把 Python 执行器输出规范化为"与进程状态分开的证据"。

接入实测教训：
* 高置信答案通道（"最终答案:" 标记）为空时，从 stdout 保守挖掘候选答案并
  打上 ``answer_source="stdout_mined"``——正确公式已打印却因缺标记被丢弃的
  事故由此堵住；
* 携带 ``code`` 时做反伪造静态检查：无实质计算却宣称 PASS 的证据一律降级
  inconclusive，不得再被仲裁当作支持性证据锁定。
"""

from __future__ import annotations

import re

from utils.output_miner import mine_stdout_answer
from utils.verdict_authenticity import assess_verification_authenticity


_STATUS_RE = re.compile(
    r"验证状态\s*[:：]\s*(PASS|FAIL|INCONCLUSIVE)", re.IGNORECASE
)
_EVIDENCE_RE = re.compile(r"验证证据\s*[:：]\s*(.+)", re.IGNORECASE)
_MAX_RATIO_RE = re.compile(
    r"(?i)(?:maximum|max(?:imum)?|最大(?:值|比值)?|ratio|f\s*\(\s*n\s*\)\s*/\s*n)"
    r"[^:\n]{0,80}[:：=]\s*([-+]?\d+(?:\.\d+)?)"
)
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_CONTRADICTION_RE = re.compile(
    r"(?:反例|矛盾|不一致|失败断言|counterexample|contradict|FAIL)",
    re.IGNORECASE,
)
_UNDEFINED_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:nan|zoo|complexinfinity)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_UNDEFINED_ANSWER_RE = re.compile(
    r"(?i)\s*(?:(?:[A-Za-z]\w*)\s*=\s*)?(?:nan|zoo|complexinfinity)\s*"
)
_EXPLICIT_COUNTEREXAMPLE_RE = re.compile(r"(?:反例|counterexample)", re.IGNORECASE)
#: 声明"零个失败/反例"的行不是反例证据，恰恰相反：它说明没有出现反例。
#:
#: 曾实测：stdout 里一行 ``fail_count: 0`` 会命中 _CONTRADICTION_RE 的 ``FAIL``
#: 子串（该正则没有词边界），整题被判成 ``evidence_status="contradict"``；
#: 下游 ``_evidence_override`` 随即用 Python 分支的答案顶掉推理分支的正确答案，
#: 产出"答案与自带步骤互相矛盾"的交付文本（推理得 42、最终写 99，而步骤里仍写
#: 42），并且白白多跑一轮 reconciliation。
#:
#: 这里只加零值防护、不收窄 _CONTRADICTION_RE：``fail_count: 3`` 这类非零计数
#: 仍然是货真价实的反例信号，必须继续被识别。
_ZERO_COUNT_RE = re.compile(
    r"(?i)"
    # 计数形态：关键词 + **白名单连接词** + 可选分隔符 + 0。
    # 两次踩坑的教训：早期允许关键词与 0 之间夹任意文本，于是
    # ``1 contradiction found at t=0`` 被整行跳过、真实反例丢失；收紧成"紧邻"后
    # 又漏掉了 ``contradictions found: 0`` / ``failures observed: 0`` /
    # ``反例的个数: 0``，这些零失败行重新被判成反例、进而用 Python 分支答案顶掉
    # 推理答案。所以连接词必须**显式白名单**，两头都要挡住。
    r"(?:"
    r"fail[_ ]?count|error[_ ]?count|mismatch[_ ]?count|"
    r"contradiction[_ ]?count|counterexample[_ ]?count|"
    r"num(?:ber)?[_ ]?of[_ ]?(?:failures?|errors?|mismatches?)|"
    r"failures?|fails|errors?|mismatches?|contradictions?|counterexamples"
    r")"
    r"(?:\s*(?:found|observed|detected|reported|counted|occurred))?"
    r"\s*[:：=]?\s*0(?![\d.])"
    r"|\b0\s+(?:failures?|fails|errors?|mismatches?|contradictions?|counterexamples)\b"
    r"|\b0\s*(?:个|处|条)?\s*(?:反例|矛盾|不一致)"
    r"|(?:反例|矛盾|不一致|失败)\s*的?\s*(?:数|个数|数量|计数|次数)?\s*(?:为|是)?\s*[:：=]?\s*0(?![\d.])"
)
_UNCERTAIN_RE = re.compile(r"未化简|无法化简|未能化简|无法判定|不能判定|outside.{0,20}domain|定义域外|"
                           r"could not simplify|unable to simplify|unevaluated|inconclusive", re.I)


def _uncertain_only(line):
    return bool(_UNCERTAIN_RE.search(line or "") and not
                (re.search(r"失败断言|assertion failed", line or "", re.I)
                 or (_EXPLICIT_COUNTEREXAMPLE_RE.search(line or "") and not
                     re.search(r"outside.{0,20}domain|定义域外", line or "", re.I))))


def _scalar(value: str):
    """从答案里取一个简单标量；复合答案返回 None。"""
    text = str(value or "")
    numbers = _NUMBER_RE.findall(text)
    if len(numbers) != 1:
        return None
    try:
        return float(numbers[0])
    except ValueError:
        return None


def _bounded(text: str, limit: int = 1000) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "...[truncated]"


def _undefined_only(line: str) -> bool:
    """Ignore undefined diagnostics unless the same line gives a finite counterexample."""
    if not _UNDEFINED_TOKEN_RE.search(line):
        return False
    # Require a tested input, an expected value, and a different finite result.
    # "counterexample x=2: expected 3, got nan" is still not a counterexample.
    return not (_EXPLICIT_COUNTEREXAMPLE_RE.search(line)
                and len(_NUMBER_RE.findall(line)) >= 3)


def parse_verification_evidence(
    output: dict | None, candidate_answer: str = "", code: str = ""
) -> dict:
    """追加保守的证据字段，不改变执行器的 success 语义。"""
    result = dict(output or {})
    stdout = str(result.get("stdout") or "")

    # 低置信答案通道：执行器只认 "最终答案:" 标记；标记缺失但 stdout 明确给出
    # 结论时（"f(2015)=3024"、"both match binomial(2k,k)^2"），把它挖出来作候选。
    # 只在高置信通道为空时启用，且不改变 success 语义。
    if not str(result.get("answer") or "").strip() and stdout:
        mined = mine_stdout_answer(stdout)
        if mined:
            result["answer"] = mined
            result["answer_source"] = "stdout_mined"

    # SymPy can return nan/zoo while the generated program exits successfully.
    # Those are undefined calculations, not mathematical answer candidates.
    answer_text = str(result.get("answer") or "").strip()
    invalid_answer = bool(_UNDEFINED_ANSWER_RE.fullmatch(answer_text))
    if invalid_answer:
        result["invalid_answer"] = answer_text
        result["answer"] = None

    marker = _STATUS_RE.search(stdout)
    marker_status = marker.group(1).upper() if marker else ""
    evidence_match = _EVIDENCE_RE.search(stdout)
    evidence_summary = evidence_match.group(1).strip() if evidence_match else ""
    invalid_evidence = _undefined_only(evidence_summary)

    contradictions = []
    for line in stdout.splitlines():
        if _STATUS_RE.search(line):
            continue
        # A comparison involving an undefined value proves neither equality nor
        # contradiction. Other, independent counterexamples remain usable.
        if _undefined_only(line) or _uncertain_only(line):
            continue
        # "零个失败"不是失败，但它**只否定自己那一段**：同一行里可能还有真实反例
        # （实测 ``0 个反例；x=2 处不一致``、``0 errors; 1 counterexample at x=2``）。
        # 所以这里剥离零计数片段后再判反例，而不是整行跳过——整行跳过会把真反例
        # 一并丢弃（早期版本的实测缺陷）。
        stripped = _ZERO_COUNT_RE.sub(" ", line)
        if _CONTRADICTION_RE.search(stripped):
            text = stripped.strip()
            if text and text not in contradictions:
                contradictions.append(text)

    if marker_status == "FAIL":
        if evidence_summary and not invalid_evidence and not _uncertain_only(evidence_summary) and evidence_summary not in contradictions:
            contradictions.insert(0, evidence_summary)
        # A bare FAIL marker is not a counterexample.

    candidate_value = _scalar(candidate_answer or result.get("answer", ""))
    if candidate_value is not None:
        for line in stdout.splitlines():
            if _undefined_only(line):
                continue
            for match in _MAX_RATIO_RE.finditer(line):
                observed = float(match.group(1))
                if observed > candidate_value + 1e-12:
                    text = match.group(0).strip()
                    if text not in contradictions:
                        contradictions.append(text)

    if contradictions:
        status = "contradict"
        if not evidence_summary or invalid_evidence:
            evidence_summary = contradictions[0]
    elif marker_status == "PASS" and not (invalid_answer or invalid_evidence):
        # 失败进程可能残留一行半截的 PASS。把它当作 inconclusive，
        # 不能借进程失败来"授权"一次匹配。success 缺失（None）同样不能
        # 视为证据支持——只有明确 success=True 的进程输出才能授权 support。
        status = "support" if result.get("success") is True else "inconclusive"
    else:
        status = "inconclusive"
        if invalid_answer or invalid_evidence:
            evidence_summary = "程序输出未定义数值，不能据此判定候选答案。"
        elif not evidence_summary and marker_status == "INCONCLUSIVE":
            evidence_summary = "程序明确声明无法判定。"

    # 反伪造：不做计算却宣称 PASS 的代码，其 support 不构成数学证据。
    # 只降级 support（contradict 的反例本身就是计算产物，不受权威引用污染）。
    authenticity = assess_verification_authenticity(code, stdout) if code else \
        {"fabricated": False, "reasons": []}
    if authenticity["fabricated"] and status == "support":
        status = "inconclusive"
        warning = "；".join(authenticity["reasons"])
        evidence_summary = f"[反伪造] {warning}。原声明不作为验证证据。"

    result.update({
        "evidence_status": status,
        "evidence_summary": _bounded(evidence_summary or stdout),
        "contradictions": contradictions,
        "evidence_marker": bool(marker),
        # 保留程序自己声明的 PASS/FAIL/INCONCLUSIVE，而不是只留下布尔值。
        # 下游需要区分“代码用反例否定了待核验候选”与“代码最后打印的
        # 自己的答案也没有通过其残差/初值检查”；后者不能升级成可交付答案。
        "verification_marker": marker_status or "UNSPECIFIED",
        "authenticity": authenticity,
    })
    return result
