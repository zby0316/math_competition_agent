# -*- coding: utf-8 -*-
"""V2 M2 置信门控：最少资源 → 最高准确率的资源分配器。

原则（用户要求：尽量用最少的资源算出更准确的答案）：
  - 分类置信度高 + 常规题型 → 单路径快速答（省 Python/双路径资源）
  - 置信度中等 → 双路验证（现状）
  - 置信度低 或 超难特征 → 深度通道信号（M3 DeepSolver 消费）

判定来源：classifier 输出的 category_confidence（0-1）。阈值可配。
"""

from __future__ import annotations

from config import CONFIG


def confidence_level(confidence: float | None) -> str:
    """把置信度映射到资源档位。

    Returns: "fast" | "standard" | "deep"
      - fast:     置信度高（≥ high_threshold）→ 单路径快速答
      - standard: 中置信 → 双路验证（现状默认）
      - deep:     低置信（< low_threshold）→ 深思考通道
    """
    if confidence is None:
        return "standard"
    thresholds = CONFIG.get("confidence_gate") or {}
    high = float(thresholds.get("high", 0.90))
    low = float(thresholds.get("low", 0.70))
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return "standard"
    if c >= high:
        return "fast"
    if c < low:
        return "deep"
    return "standard"


def fast_lane_eligible(question_mode: str, confidence: float | None,
                       difficulty: str = "", problem: str = "") -> bool:
    """V2.1 M8 FastLane：L1 客观题高置信 → 单路径快速答。

    满足全部条件才走快速通道（省 Python/critic，时间给难题）：
      - 题型为客观题（选择/判断/概念填空，计算填空排除）；
      - 置信度 fast 档（≥ high_threshold）；
      - 难度为 L1/L2（非 hard 显式标记，防止难题被误省）。
    实算填空永远不省 Python（治本机制，真机实证 教训）。
    """
    if question_mode in ("choice", "true_false"):
        pass
    elif question_mode == "fill":
        from utils.verify_router import needs_python_verify
        if needs_python_verify(problem or "", question_mode):
            return False
    else:
        return False
    if confidence_level(confidence) != "fast":
        return False
    if str(difficulty or "").strip().lower() in ("hard", "very_hard", "extremely_hard"):
        return False
    return True


def wants_deep_solver(question_mode: str, confidence: float | None,
                      category: str = "", difficulty: str = "") -> bool:
    """该题是否值得进深度通道（M3 DeepSolver）。

    触发：低置信（< low_threshold）或 高难度领域 或 显式标记超难。
    客观题不做深思考（快速路径就是最优解），计算/证明/推导题才需要。
    """
    if question_mode in ("choice", "true_false"):
        return False
    if confidence_level(confidence) == "deep":
        return True
    if category in (CONFIG.get("deep_solver_domains") or []):
        return True
    if str(difficulty or "").strip().lower() in ("hard", "very_hard", "extremely_hard"):
        return True
    return False
