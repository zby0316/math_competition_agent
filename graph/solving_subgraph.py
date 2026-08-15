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

    成本纪律：第二路径是一次完整推理调用（77-550s），不能无条件扇出——
    - 首轮：高风险领域（组合/竞赛构造/微分几何/测度积分）随首路径并行发出；
      V2.5 缺口1扩展：hard 难度 或 分类置信 <0.85 时，**无论哪个领域**都扇出
      ——异构方法互验是打破"单样本系统性错误"的唯一机制，不该只限 4 领域。
    - 调解重算轮：一律追加（此时已知首路径失败，多样性正是缺的东西）；
    - 客观题、时间 fast path、配置关闭时不发。
    - V2 M2：分类置信度高（≥0.90，fast 档）时即使高风险领域也不扇出——
      首答已高度可信，第二路径是浪费资源（最少资源原则）。
    """
    if not CONFIG.get("enable_diverse_reasoning", True):
        return False
    if is_objective_mode(state.get("question_mode", "")):
        return False
    if state.get("reconciliation_round", 0) >= 1:
        return True
    # V2 M2 置信门控：fast 档（高置信）省第二路径
    from utils.confidence_gate import confidence_level
    if confidence_level(state.get("category_confidence")) == "fast":
        return False
    category = state.get("category", "")
    # V2.5 缺口1：hard 难度或低置信（<0.85）→ 全领域扇出 B 路径。
    # 原 4 领域白名单仍保留：medium 档下只有这 4 个单样本错误率最高的领域扇出。
    difficulty = str(state.get("difficulty") or "").strip().lower()
    if difficulty == "hard":
        return True
    if (state.get("category_confidence") or 0) < 0.85:
        return True
    if category not in (CONFIG.get("high_risk_domains") or []):
        return False
    try:
        clock = get_deps(config).time_budget
        if clock is not None and clock.fast_path():
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
