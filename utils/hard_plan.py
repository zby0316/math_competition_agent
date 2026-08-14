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
【难题·完整求解与自洽验证】本题难度较高，请在本次回答内一次完成以下七点：
1. 目标锚定：一句话写清"本题要证明/求出的结论是什么"，不要答非所问。
2. 路径规划：列出要用的定理/方法/构造（如中值定理、谱定理、生成函数），
   并说明为什么选它（一句即可）。
3. 逐步求解：按规划逐步推导，每步给出关键依据（引用的定理名或定义）。
4. 结构完整：证明/推导题务必"定理陈述 → 关键步骤链 → 显式结论"三段式，
   结论单独成行写"因此……成立"（judger 依赖显式结论判分）。
5. 覆盖全面：逐项核对题目所有要求——"证明 X 并说明 Y"是两件事，都要答；
   "求 A 与 B"要两个都给出，不漏任何一项。
6. 逻辑自检：每一步都能从前一步严格推出；引用的定理适用当前条件；
   结论确实由前文推出。发现跳跃就补全。
7. 自洽验证：关键结论用至少一种独立方式交叉核对——计算题把结果代回原方程
   或取特例验证；计数题用另一种计数方法（如补集/递推）复核；证明题用小规模
   特例检验结论；若验证发现矛盾，回头修正主推导。
注意：若某一步无法严格论证，诚实标注"此处需进一步说明"并给出充分条件；
宁可少一步，也要保证每一步可核、每一项都覆盖、结论经得起验证。
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
