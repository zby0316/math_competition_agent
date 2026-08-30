"""对账轮共享策略：校验与重试路由共用。

对账轮会把整个求解子图重跑一遍——再一次推理调用加再一次 Python 调用。在这个
模型上这是最贵的动作（单次推理调用在中等难度题上实测 77-116s，难题更长），
所以轮数上限同时受两个预算约束：token 和墙钟。买一次跑不完的重跑，严格劣于
直接用手头已有的答案作答。
"""

from config import CONFIG
from utils.deps import get_deps
from utils.retry_pricing import DEFAULT_RETRY_COST_S, last_attempt_cost


def _deps(config):
    try:
        return get_deps(config)
    except Exception:  # noqa: BLE001 - 测试里可能传入裸 config。
        return None


def reconciliation_round_limit(config) -> int:
    deps = _deps(config)
    budget = getattr(deps, "token_budget", None)
    clock = getattr(deps, "time_budget", None)
    if clock is not None and clock.fast_path():
        # 容不下再一次完整求解；只允许已经完成的那一轮。
        return 1
    if budget and budget.is_tight():
        return 1
    return CONFIG["reconciliation_max_rounds"]


def projected_solving_seconds(time_budget) -> float:
    """依据已观测耗时估算再来一轮并行推理/Python 的耗时。"""
    if time_budget is None:
        return 0.0
    return max(
        last_attempt_cost(time_budget, "reasoning"),
        last_attempt_cost(time_budget, "python"),
        DEFAULT_RETRY_COST_S,
    )


def reconciliation_retry_available(state, config) -> bool:
    """进入对账是否还能再产生一轮求解运行。"""
    completed_rounds = state.get("reconciliation_round", 0)
    if completed_rounds + 1 >= reconciliation_round_limit(config):
        return False
    deps = _deps(config)
    clock = getattr(deps, "time_budget", None)
    return clock is None or clock.can_afford(projected_solving_seconds(clock))
