# -*- coding: utf-8 -*-
"""V2.1 M7 ProofDeepener —— 证明/推导题的深加工引擎。

问题（V2 真机评测实证）：L3/L4 证明题答案"看着完整"却大量被判 wrong
（76%/58% 正确率）。判分失败多为证明结构不满足官方 judger 的
"关键步骤链 + 结论"判定口径——答案有数学内容但步骤跳跃/结论不显式。

设计：对证明/推导题在成稿前做**结构强制**：
  1. 证明三段式模板：定理陈述 → 关键步骤链（编号）→ 显式结论
  2. 完整性自检（一次 prefill LLM，~10s）：检测步骤跳跃/结论缺失
  3. 缺失补强（~512 token）：补写缺失步骤或显式结论

与 build_proof_body 的关系：build_proof_body 从 reasoning 结果拼装正文，
ProofDeepener 是**在最终答案定型前的结构审计+补强**，二者串行。
"""

from __future__ import annotations

import logging
import re

from config import CONFIG

logger = logging.getLogger("proof_deepener")

#: 显式结论标记（judger 认这些词）——检测"结尾 120 字符内"是否出现，
#: 避免把推理中间的"所以/因此/于是"误判为结论（2026-08-12 修正）。
_CONCLUSION_RE = re.compile(
    r"(?:证毕|得证|故得证|综上所述|证得|Q\.?E\.?D|□)", re.IGNORECASE)
_CONCLUSION_TAIL_RE = re.compile(
    r"(?:因此|所以|结论[：:]|故)[^。；\n]{0,30}(?:成立|得证|证毕|为[^。；\n]{0,20}$)",
    re.IGNORECASE)


def _has_conclusion(text: str) -> bool:
    """判断是否含显式结论：强标记全文 / 弱标记看结尾 120 字符。"""
    if _CONCLUSION_RE.search(text):
        return True
    tail = text[-120:]
    return bool(_CONCLUSION_TAIL_RE.search(tail))


def is_proof_question(problem: str, ptype: str = "") -> bool:
    """判断是否为证明/推导题（需要结构模板）。"""
    if str(ptype or "").strip().lower() == "proof":
        return True
    text = str(problem or "")
    return bool(re.search(
        r"(?:证明|求证|推导|论证|证得|show that|prove that|deduce|"
        r"derive|验证.*成立|说明.*成立)", text, re.IGNORECASE))


def proof_structure_check(answer: str, problem: str) -> dict:
    """证明结构完整性检查（零成本正则先行）。

    Returns:
      {"complete": bool, "has_statement": bool, "has_steps": bool,
       "has_conclusion": bool, "reasons": list[str]}
    """
    text = str(answer or "")
    # 结论标记（强标记全文 / 弱标记看结尾）
    has_conclusion = _has_conclusion(text)
    # 步骤信号：编号步骤/逻辑连接词/推导符号
    has_steps = bool(re.search(
        r"(?:步骤\s*[0-9一二三四五]|第一步|其次|然后|因为|由于|由.*得|"
        r"=>|⟹|∴|⇒|→|1\.\s*[A-Za-z（(])", text))
    # 定理/定义引用
    has_statement = bool(re.search(
        r"(?:设|令|已知|由题设|定理|定义|假设|设\s*[A-Za-zεδ]|"
        r"设\s*[a-zA-Z]+\s*[∈(])", text))
    reasons = []
    if not has_statement:
        reasons.append("缺定理陈述/假设设定")
    if not has_steps:
        reasons.append("缺关键步骤链（编号或推导连接词）")
    if not has_conclusion:
        reasons.append("缺显式结论标记（证毕/因此/结论）")
    return {
        "complete": bool(has_statement and has_steps and has_conclusion),
        "has_statement": has_statement,
        "has_steps": has_steps,
        "has_conclusion": has_conclusion,
        "reasons": reasons,
    }


def deepen_proof(client, deps, problem: str, answer: str,
                 max_refine_rounds: int = 1) -> str:
    """证明结构补强：检测缺失 → LLM 补写 → 返回补强后的完整证明。

    Args:
        client: LLM client（与 coordinator 相同）
        deps: 依赖（含 time_budget / logger）
        problem: 题面
        answer: 当前证明答案
        max_refine_rounds: 补强轮数（默认 1，够用）

    Returns:
        补强后的证明（失败时返回原 answer）
    """
    if not CONFIG.get("enable_proof_deepener", True):
        return str(answer or "")
    text = str(answer or "").strip()
    if not text:
        return text
    check = proof_structure_check(text, problem)
    if check["complete"]:
        return text
    try:
        from utils.llm_retry import chat_with_retry
        from utils.cot_stripper import strip_cot_prefix
        prompt = (
            "你是一位严谨的数学证明审稿人。下面是一道证明题和一份已有的证明答案。\n\n"
            f"【题目】\n{str(problem)[:800]}\n\n"
            f"【已有证明】\n{text[:1500]}\n\n"
            f"【结构缺陷】{'; '.join(check['reasons']) if check['reasons'] else '结构不完整'}\n\n"
            "请输出补强后的完整证明，必须满足三段式结构：\n"
            "1. 定理陈述：重述题设与要证的结论（以『证明：』或『求证：』开头）；\n"
            "2. 关键步骤链：编号步骤（步骤1/步骤2/...），每步引用已知定理或定义；\n"
            "3. 显式结论：以『证毕』或『因此...成立，证毕』收尾。\n"
            "不要引入原答案没有的新数学内容，只做结构补全。"
        )
        raw = chat_with_retry(
            client, [{"role": "user", "content": prompt}],
            temperature=0.0, max_tokens=1024,
            logger=deps.logger, time_budget=deps.time_budget,
            label="proof_deepener",
        )
        refined = strip_cot_prefix(raw or "").strip()
        # 保留原答案的数学核心 + 补强结构：若补强版太短或退化，用原答案
        if len(refined) < 40 or not _CONCLUSION_RE.search(refined):
            logger.info("Proof deepener 补强结果退化，保留原答案")
            return text
        return refined
    except Exception as exc:  # noqa: BLE001 - 补强失败保持原答案
        logger.warning("Proof deepener failed: %s", exc)
        return text


# ---- V2.3 M12 证明自检（Proof Self-Critique） ----

_SELF_CRITIQUE_PROMPT = (
    "你是一位严谨的数学审稿人。下面是一道数学题和一份答案。\n\n"
    "【题目】\n{problem}\n\n"
    "【答案】\n{proof}\n\n"
    "请严格审查，只输出一行 JSON：\n"
    '{{"sound": true|false, "gap": "若 sound=false，指出最关键的一处逻辑跳跃或漏洞（30字内）；否则写 ok", '
    '"coverage": true|false, "missing": "若 coverage=false，指出题目要求但答案未覆盖的部分（30字内）；否则写 ok"}}\n'
    "两项判定标准：\n"
    "① sound（逻辑）：每一步是否可从前一步严格推出；引用的定理是否适用当前条件；结论是否由前文推出。\n"
    "② coverage（覆盖，V2.3 M13）：题目问了几件事（如『证明X并说明Y』是两件事），答案是否全部覆盖；"
    "『并/且/同时/以及/说明/求...与...』等连接词提示多问，逐项核对是否都答了。\n"
    "注意：只找『逻辑漏洞』和『漏答』，不找『表述瑕疵』——数学成立且全覆盖就 sound=true 且 coverage=true。"
)


def proof_self_critique(client, deps, problem: str, proof: str,
                        max_fix_rounds: int = 1) -> str:
    """V2.3 M12+M13 答案自检：审稿查逻辑漏洞 + 题面覆盖。

    与 M7（deepen_proof，补结构）的分工：
      - M7 补"三段式结构"（定理陈述/步骤链/结论是否齐全）；
      - M12 查"逻辑正确性"（每步能否严格推出，定理是否适用）；
      - M13 查"题面覆盖"（题目问了几件事，答案是否全答——救 partial 类漏答，
        如 Hahn-Banach 题漏答『几何意义』）。

    成本：一次 prefill（~10s）；发现实质缺陷时再一轮补强（~512 token）。
    封顶 1 轮补强，绝不无限循环（官方 20min 硬限）。
    """
    if not CONFIG.get("enable_proof_self_critique", True):
        return str(proof or "")
    text = str(proof or "").strip()
    if not text:
        return text
    try:
        from utils.llm_retry import chat_with_retry
        from utils.cot_stripper import strip_cot_prefix
        raw = chat_with_retry(
            client,
            [{"role": "user", "content": _SELF_CRITIQUE_PROMPT.format(
                problem=str(problem)[:800], proof=text[:2000])}],
            temperature=0.0, max_tokens=320,
            logger=deps.logger, time_budget=deps.time_budget,
            label="proof_self_critique",
        )
        cleaned = strip_cot_prefix(raw or "").strip()
        # 解析 JSON 判定
        import json as _json
        import re as _re
        sound = None
        gap = ""
        coverage_ok = True
        missing = ""
        for m in _re.finditer(r"\{[^{}]*\}", cleaned):
            try:
                obj = _json.loads(m.group(0))
                if "sound" in obj:
                    sound = bool(obj.get("sound"))
                    gap = str(obj.get("gap", "")).strip()
                if "coverage" in obj:
                    coverage_ok = bool(obj.get("coverage"))
                    missing = str(obj.get("missing", "")).strip()
            except Exception:  # noqa: BLE001
                continue
        if sound is True and coverage_ok:
            return text
        # 有实质漏洞或漏答 → 补强一轮（把缺陷清单带进修复提示）
        defects = []
        if sound is False and gap:
            defects.append(f"逻辑漏洞：{gap}")
        if not coverage_ok and missing:
            defects.append(f"漏答部分：{missing}")
        if max_fix_rounds > 0 and defects:
            from utils.llm_retry import chat_with_retry as _cwr
            fix_prompt = (
                "下面是一道数学题和一份答案。审稿人指出以下缺陷：\n\n"
                + "\n".join(f"· {d}" for d in defects) + "\n\n"
                f"【题目】{str(problem)[:600]}\n\n"
                f"【原答案】\n{text[:1500]}\n\n"
                "请修复上述缺陷，输出完整答案（保持原有正确部分，修正逻辑漏洞、补全漏答部分），"
                "以『证毕』或『因此…成立，证毕』收尾。"
            )
            fixed = _cwr(
                client, [{"role": "user", "content": fix_prompt}],
                temperature=0.0, max_tokens=1024,
                logger=deps.logger, time_budget=deps.time_budget,
                label="proof_self_critique_fix",
            )
            fixed_clean = strip_cot_prefix(fixed or "").strip()
            if len(fixed_clean) >= 40 and _CONCLUSION_RE.search(fixed_clean):
                return fixed_clean
        return text
    except Exception as exc:  # noqa: BLE001 - 自检失败保持原答案
        logger.warning("Proof self-critique failed: %s", exc)
        return text
