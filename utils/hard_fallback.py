# -*- coding: utf-8 -*-
"""V2.3 M14 难题兜底 —— 模型"放弃"时给出部分答案。

背景（2026-08-13 V2.3 验收）：第二套纯 L3/L4 题集，真机实证 Van der Waerden
W(2,3)=9 模型输出"声称无法完成"，被判 0 分。这类题即使不能完整证明，
给出"可证明的特殊情形 + 证明思路框架 + 部分结论"也能让 judger 给 partial，
远好于交白卷。

设计：检测到 final_response 为"放弃型"（无法/无法确定/无法完成/缺少条件等）时，
触发一次"部分求解兜底"（prefill ~10s + 一次推理 ~60s）：
  1. 要求模型给出能证的特殊情形（小参数实例）；
  2. 证明思路/关键步骤框架（哪怕不完整）；
  3. 部分结论或猜测（显式标注"部分/猜测"）。

成本：仅放弃题触发（占比极低），封顶 1 次兜底，不无限重试。
"""
from __future__ import annotations

import logging
import re

from config import CONFIG

logger = logging.getLogger("hard_fallback")

#: 放弃型答案的判定（模型自己说做不出来/缺条件/无法确定）
_GIVE_UP_RE = re.compile(
    r"(?:无法|不能|缺少|缺失|信息不足|无法确定|无法完成|无法求解|"
    r"不可判定|无从|请补充|无法给出|无法作答|无法计算)",
    re.IGNORECASE)

_PARTIAL_FALLBACK_PROMPT = (
    "下面是一道数学题。此前解题未给出完整答案。\n\n"
    "【题目】\n{problem}\n\n"
    "请尽可能给出部分解答，即使无法完整求解也要尽力产出，按以下结构：\n"
    "1. 可证明的特殊情形：取最小的/最简单的参数，实际求解并给出明确结果；\n"
    "2. 证明思路框架：列出解决本题需要的关键步骤/定理（哪怕只到一半）；\n"
    "3. 部分结论：能确定的任何中间结论或最终结论的猜测（显式标注『部分结论/猜测』）。\n"
    "不要写『无法完成』——任何部分进展都比空白有价值。"
)


def looks_like_give_up(answer: str) -> bool:
    """判断答案是否为"放弃型"（模型自己说做不出来）。"""
    s = str(answer or "").strip()
    if not s:
        return True
    # 答案很短且含放弃词 → 放弃
    if len(s) < 200 and _GIVE_UP_RE.search(s):
        return True
    # 长答案但以"无法"开头（如"最终答案：无法确定"）
    if s.startswith(("无法", "无法确定", "无法完成", "缺少")):
        return True
    return False


def partial_fallback(client, deps, problem: str, answer: str) -> str:
    """对放弃型答案做一次"部分求解兜底"，返回更好的部分答案。

    失败或已非放弃型时返回原 answer。
    """
    if not CONFIG.get("enable_hard_fallback", True):
        return str(answer or "")
    text = str(answer or "").strip()
    if not looks_like_give_up(text):
        return text
    try:
        from utils.llm_retry import chat_with_retry
        from utils.cot_stripper import strip_cot_prefix
        raw = chat_with_retry(
            client,
            [{"role": "user", "content": _PARTIAL_FALLBACK_PROMPT.format(
                problem=str(problem)[:1000])}],
            temperature=0.2, max_tokens=1200,
            logger=deps.logger, time_budget=deps.time_budget,
            label="hard_fallback",
        )
        refined = strip_cot_prefix(raw or "").strip()
        if len(refined) >= 40 and not looks_like_give_up(refined):
            return refined
        return text
    except Exception as exc:  # noqa: BLE001 - 兜底失败保持原答案
        logger.warning("Hard fallback failed: %s", exc)
        return text
