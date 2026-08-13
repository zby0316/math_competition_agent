# -*- coding: utf-8 -*-
"""V2.3 初解质量强化 —— 超难题"计划先行"提示注入。

设计背景（2026-08-13）：
  V2 全量 93 题实测，8 个"真错"里 7 个是"答案数学正确但被判 wrong"（判分口径），
  只有 1 个（idx=80 Riesz 定理答成"Radon 测度版"而非"L^p 对偶版"）是真答偏。
  V2.1 抽样 10/10 全对证明 M7（证明结构）已解决"结构不被认"。

  超难题真正可提升的，是"第一次就答对"——而不是靠串行重解（官方慢 API 下单次
  reasoning 可到 8-13 分钟，串行重解必然超时，见 V2.3 方案论证）。

设计：对超难题（L3/L4 或分类低置信）在 reasoning 提示词后追加一段
  "计划先行"元认知引导，让模型在**单次调用内**完成：
    ① 先想清楚"要证什么/求什么"（目标锚定）
    ② 再规划"用什么定理/方法/步骤"（路径规划）
    ③ 然后逐步求解并自查每步是否可核（过程自检）
  这与 REASONING_PROMPT 已有的"题面口径检查"互补：口径检查是"审题"，
  计划先行是"解题策略"——后者正是超难题缺失的一环。

成本：仅超难题触发（~15-20% 题），纯提示词追加，0 额外 API 调用。
"""

from __future__ import annotations


#: 超难题"计划先行"引导（追加到 reasoning 提示词末尾）。
#: 强调"单次调用内完成三步"，不引导多轮（避免与 20min 硬限冲突）。
HARD_PLAN_PROMPT = """

【超难题·计划先行】本题难度较高，请按下面三步在本次回答内一次完成：
1. 目标锚定：先用一句话写清"本题要求证/求的结论是什么"（不要省略目标，不要答非所问）。
2. 路径规划：列出解题要用到的定理/方法/构造（如"中值定理""谱定理""构造测度"
   "生成函数"），并说明为什么选它（一句即可）。
3. 逐步求解：按规划逐步推导，每步给出关键依据（引用的定理名或定义），
   最后回扣第 1 步的目标，确认已完整回答。
注意：若某一步无法严格论证，不要跳过——诚实标注"此处需进一步说明"，
并给出能成立的充分条件；宁可少一步也要保证每一步可核。
"""


def hard_plan_hint(question_mode: str, confidence: float | None,
                   difficulty: str = "", level: int | None = None) -> str:
    """判断是否注入"计划先行"提示，返回提示片段或空串。

    触发条件（全部满足才注入）：
      - 难度显式 hard / level >= 3 / 分类低置信（任一）；
      - 非客观题（选择/判断不需要计划）。
    """
    if question_mode in ("choice", "true_false"):
        return ""
    hard = str(difficulty or "").strip().lower() in (
        "hard", "very_hard", "extremely_hard")
    if level is not None:
        hard = hard or int(level) >= 3
    if confidence is not None and confidence < 0.6:
        hard = True
    return HARD_PLAN_PROMPT if hard else ""


def wants_plan(question_mode: str, confidence: float | None,
               difficulty: str = "", level: int | None = None) -> bool:
    """布尔版，供 trace 留痕。"""
    return bool(hard_plan_hint(question_mode, confidence, difficulty, level))
