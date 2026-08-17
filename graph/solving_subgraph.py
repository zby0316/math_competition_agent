from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from state.math_agent_state import MathAgentState
from nodes import reasoning_agent_node, python_agent_node, cross_validator_node
from utils.error_handler import node_wrapper
from utils.problem_profile import is_objective_mode
from utils.deps import get_deps
from config import CONFIG


def _want_diverse_branch(state, config) -> bool:
    """异构第二推理路径的触发条件（VeritasMath）。

    V2.9 方法差距修复（retry=0 根因）：不再用「分类器置信度 / 领域白名单」
    限制异构验证——分类器把科研级题判成 easy+0.98 高置信（实证），fast 档
    短路会让这些题只有单路径 A，答错就错、没有第二视角（官方 retry=0 的根因
    之一）。异构互验是打破「单样本系统性错误」的唯一机制，所有非客观题都该
    有第二视角。
    - 客观题：不扇出（快速路径就是最优解，第二视角无增量）；
    - 调解重算轮：一律追加（已知首路径失败，多样性正是缺的东西）；
    - 时间 fast path：剩余时间不足以再跑一次完整推理时不扇出（唯一保留的
      成本护栏——分类器信号不可靠，但时间信号是硬的）；
    - 配置关闭时不发。
    """
    if not CONFIG.get("enable_diverse_reasoning", True):
        return False
    if is_objective_mode(state.get("question_mode", "")):
        return False
    if state.get("reconciliation_round", 0) >= 1:
        return True
    try:
        clock = get_deps(config).time_budget
        # 不能用 clock.fast_path()：它基于单题软预算（remaining = soft_total -
        # reserve），easy 题软预算 480s、reserve 300s → remaining 一开始就只有
        # ~180s < 阈值 300s → fast_path 恒 True，B 路径永远不扇出。而被分类器
        # 判 easy 的科研级题恰好是 retry=0 的重灾区——软预算信号在这里是错的。
        # B 路径是一次 prefill 级推理（~30-60s），只看单题硬预算（1200s）是否
        # 还够，硬预算剩余 < 90s 才跳过（此时确实没空间跑第二路径了）。
        if clock is not None and clock.remaining_hard() < 90:
            return False
    except Exception:  # noqa: BLE001 - 取不到预算时按可负担处理
        pass
    return True


def build_solving_subgraph():
    sub = StateGraph(MathAgentState)
    sub.add_node("reasoning_agent", node_wrapper(reasoning_agent_node, "reasoning_agent"))
    sub.add_node("python_agent", node_wrapper(python_agent_node, "python_agent"))
    sub.add_node("cross_validator", node_wrapper(cross_validator_node, "cross_validator"))

    def fan_out(state, config):
        reasoning_send = Send(
            "reasoning_agent", {**state, "branch_hint": state.get("reasoning_retry_hint"),
                                "method_variant": ""}
        )
        sends = [reasoning_send]
        # V2.1 M8 FastLane：L1 客观题高置信 → 单路径快速答
        # （省 Python/critic，时间给难题）。实算填空/难题不触发。
        from utils.confidence_gate import fast_lane_eligible
        if fast_lane_eligible(
                state.get("question_mode", "computation"),
                state.get("category_confidence"),
                state.get("difficulty", ""),
                state.get("problem", "")):
            return sends
        # V2 M3 DeepSolver：深思考信号时强制扇出 B 路径（第二候选），
        # 供 cross_validator 多数一致合并（超难题单候选不够）。
        from utils.confidence_gate import wants_deep_solver
        if wants_deep_solver(
                state.get("question_mode", "computation"),
                state.get("category_confidence"),
                state.get("category", ""),
                state.get("difficulty", "")):
            sends.append(Send(
                "reasoning_agent", {**state, "branch_hint": state.get("reasoning_retry_hint"),
                                    "method_variant": "B"}
            ))
        elif _want_diverse_branch(state, config):
            sends.append(Send(
                "reasoning_agent", {**state, "branch_hint": state.get("reasoning_retry_hint"),
                                    "method_variant": "B"}
            ))
        question_mode = state.get("question_mode", "computation")
        # V2 M2 置信门控：高置信纯概念客观题跳过 Python 验证（省资源）
        from utils.confidence_gate import can_skip_python_verify, wants_deep_solver
        if can_skip_python_verify(question_mode, state.get("category_confidence"),
                                  state.get("problem", "")):
            return sends
        # V2 M3 DeepSolver：深思考信号时即使客观题也保留 Python（超难题要机器证据）
        deep_triggered = wants_deep_solver(
            question_mode, state.get("category_confidence"),
            state.get("category", ""), state.get("difficulty", ""))
        if not deep_triggered and is_objective_mode(question_mode):
            from utils.verify_router import needs_python_verify
            if not needs_python_verify(state.get("problem", ""), question_mode):
                # 纯概念客观题：短路径自带核对，Python 标量无法表达多空/选项，跳过
                return sends
            # 实算填空：继续扇出 Python 验证分支（双路）
        sends.append(
            Send("python_agent", {**state, "branch_hint": state.get("python_retry_hint")})
        )
        return sends
    sub.add_conditional_edges(START, fan_out, ["reasoning_agent", "python_agent"])
    sub.add_edge("reasoning_agent", "cross_validator")
    sub.add_edge("python_agent", "cross_validator")
    sub.add_edge("cross_validator", END)
    return sub.compile()
