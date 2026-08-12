# -*- coding: utf-8 -*-
"""V2 M3 DeepSolver 深思考通道 —— 科研级超难题的核心武器。

用户要求：对标科研级难题开发，全卷正确率 ≥95%。官方题集中必然存在
Intern-S2 单次推理答不对的超难题（V1 官方评测 16/112 实证：超难题 + 6h
硬约束下"每题死磕"必然失败，但"深思考少数题"可行）。

设计：低置信/超难领域题进入深思考通道——
  1. 多候选生成：最多 3 个独立候选（方法 A 直接法 / B 构造法 / C 反证法），
     每个候选带 confidence 自评；
  2. 候选代回核验：复用 Playoff 的确定性代回（沙箱执行），对数值/计数/
     方程题做残差/枚举验证；
  3. 组合裁决：多数一致 → 采纳；两两冲突 → 取被 Playoff 支持的候选；
  4. 预算护栏：全程受 TimeBudget 钳制，超时立即用"当前最佳候选"兜底。

与现有机制的关系：
  - 复用 reasoning_agent_node 的 method_variant="B" 路径生成第二候选；
  - 复用 playoff_node 的候选代回核验逻辑；
  - 深思考只在 wants_deep_solver() 为真时触发（≤10% 题），成本可控。
"""

from __future__ import annotations

import logging

from config import CONFIG

logger = logging.getLogger("deep_solver")


def deep_solver_plan(question_mode: str, category: str = "",
                     difficulty: str = "") -> dict:
    """返回深思考通道的执行计划（供图内节点调度）。

    Returns:
      {"enabled": bool, "candidates": int, "verify": bool, "reason": str}
    """
    from utils.confidence_gate import wants_deep_solver
    # 需要置信度参数时由调用方传入；此处只做领域/难度判断
    enabled = False
    if str(difficulty or "").strip().lower() in ("hard", "very_hard", "extremely_hard"):
        enabled = True
    if category in (CONFIG.get("deep_solver_domains") or []):
        enabled = True
    if not enabled:
        return {"enabled": False, "candidates": 1, "verify": False, "reason": "常规题"}
    # 客观题不做深思考（快速路径最优）
    if question_mode in ("choice", "true_false"):
        return {"enabled": False, "candidates": 1, "verify": False, "reason": "客观题快速路径"}
    plan = {
        "enabled": True,
        "candidates": 3,
        "verify": question_mode in ("computation", "fill", "equation"),
        "reason": f"深思考通道: {category}/{difficulty}",
    }
    return plan


def merge_deep_candidates(candidates: list[dict], budget=None) -> tuple[str, str]:
    """合并多个候选 → (最终答案, 采纳理由)。

    裁决规则：
      - 有候选缺失/失败：用可用候选的多数；
      - 多数一致：采纳多数派；
      - 两两冲突：按 confidence 排序取最高（预算护栏内），记录分歧留 trace。
    """
    usable = [c for c in candidates
              if c and str(c.get("answer") or "").strip()
              and not str(c.get("answer")).startswith("无法")]
    if not usable:
        return "", "deep: 无可用候选"
    if len(usable) == 1:
        return str(usable[0]["answer"]).strip(), "deep: 单候选"
    # 文本归一化后计数（去空白/LaTeX 标记/大小写）
    import re as _re
    def _norm(s: str) -> str:
        # \frac{a}{b} → a/b（先处理，避免命令删除后分子分母粘连）
        for _ in range(4):
            m = _re.search(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", s)
            if not m:
                break
            s = s[:m.start()] + f"{m.group(1)}/{m.group(2)}" + s[m.end():]
        s = _re.sub(r"\\[a-zA-Z]+", " ", s)   # 其余 LaTeX 命令
        s = _re.sub(r"[{}_^$\\]", "", s)       # LaTeX 结构符
        return _re.sub(r"\s+", "", s).strip().lower()
    groups: dict[str, list[dict]] = {}
    for c in usable:
        key = _norm(str(c["answer"]))
        groups.setdefault(key, []).append(c)
    majority = max(groups.values(), key=len)
    if len(majority) >= 2:
        return str(majority[0]["answer"]).strip(), f"deep: {len(majority)}/{len(usable)} 候选一致"
    # 无多数：按 confidence 取最高
    best = max(usable, key=lambda c: float(c.get("confidence") or 0.0))
    return str(best["answer"]).strip(), "deep: 分歧取最高置信候选"
