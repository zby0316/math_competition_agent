from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from state.math_agent_state import MathAgentState
from nodes import (
    reasoning_agent_node, python_agent_node, cross_validator_node,
    solver_decision_node,
)
from utils.error_handler import node_wrapper
from utils.problem_profile import is_objective_mode
from config import CONFIG


def _want_diverse_branch(state, config):
    """异构第二推理路径的触发条件（VeritasMath）。

    成本纪律：第二路径是一次完整推理调用（77-550s），不能无条件扇出——
    - 首轮：只对高风险领域（组合/竞赛构造/微分几何/测度积分，单样本错误率
      最高的四类）随首路径并行发出；
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
    from utils.confidence_gate import confidence_level
    if confidence_level(state.get("category_confidence")) == "fast":
        return False
    category = state.get("category", "")
    if category not in (CONFIG.get("high_risk_domains") or []):
        return False
    return True


def _want_b_in_light_mode(state, config):
    """V3.0：light（python 确定性成功后的轻推理）模式下的 B 路径触发。

    light 已省采样投票/重试，但异构多样性在高风险领域/hard 题/重算轮仍有
    独立价值（python 与 light 推理同源，可能一致地错）——保留 B 路径做
    第三独立证据，成本可控（单次轻调用）。
    """
    if not CONFIG.get("enable_diverse_reasoning", True):
        return False
    if is_objective_mode(state.get("question_mode", "")):
        return False
    if state.get("reconciliation_round", 0) >= 1:
        return True
    if str(state.get("difficulty") or "").lower() in {"hard", "very_hard", "extremely_hard"}:
        return True
    category = state.get("category", "")
    if category in (CONFIG.get("high_risk_domains") or []):
        return True
    return False


def build_solving_subgraph():
    """V3.0 python-first 拓扑。

    START ─(计算题/实算填空)─► python_agent ─► solver_decision ─► reasoning(A/B) ─► cross_validator
          └(纯概念客观题)────► solver_decision（跳过 python）

    solver_decision 判 python 确定性成功后置 reasoning_light：推理降为单次独立
    复核（省采样投票/重试），B 路径仅高风险/hard/重算轮保留。
    python 失败/非计算题 → 完整推理路径（fast lane / deep solver / diverse
    逻辑原样保留在 fan_out_reasoning 的 full 分支）。
    """
    sub = StateGraph(MathAgentState)
    sub.add_node("reasoning_agent", node_wrapper(reasoning_agent_node, "reasoning_agent"))
    sub.add_node("python_agent", node_wrapper(python_agent_node, "python_agent"))
    sub.add_node("solver_decision", node_wrapper(solver_decision_node, "solver_decision"))
    sub.add_node("cross_validator", node_wrapper(cross_validator_node, "cross_validator"))

    def fan_in(state, config):
        """V3.0：python 先行。Send 保留 python_retry_hint 注入通道。"""
        question_mode = state.get("question_mode", "computation")
        if is_objective_mode(question_mode):
            from utils.verify_router import needs_python_verify
            if needs_python_verify(state.get("problem", ""), question_mode):
                # 实算填空：python 是第二证据，先跑
                return [Send("python_agent", {**state,
                                              "branch_hint": state.get("python_retry_hint")})]
            # 纯概念客观题：无需 python，直入决策（决策对客观题返回空 → full 路径）
            return ["solver_decision"]
        return [Send("python_agent", {**state,
                                      "branch_hint": state.get("python_retry_hint")})]
    sub.add_conditional_edges(START, fan_in, ["python_agent", "solver_decision"])
    sub.add_edge("python_agent", "solver_decision")

    def fan_out_reasoning(state, config):
        """solver_decision 后推理扇出：light（单次复核）或 full（旧逻辑）。"""
        hint = state.get("reasoning_retry_hint")
        if bool(state.get("reasoning_light")):
            sends = [Send("reasoning_agent",
                          {**state, "branch_hint": hint, "method_variant": "",
                           "reasoning_light": True})]
            if _want_b_in_light_mode(state, config):
                sends.append(Send("reasoning_agent",
                                  {**state, "branch_hint": hint, "method_variant": "B",
                                   "reasoning_light": True}))
            return sends
        # ---- full 路径（旧 fan_out 逻辑原样保留，python 已在决策前跑完）----
        reasoning_send = Send("reasoning_agent",
                              {**state, "branch_hint": hint, "method_variant": ""})
        sends = [reasoning_send]
        # V2.1 M8 FastLane：L1 客观题高置信 → 单路径快速答
        from utils.confidence_gate import fast_lane_eligible
        if fast_lane_eligible(
                state.get("question_mode", "computation"),
                state.get("category_confidence"),
                state.get("difficulty", ""),
                state.get("problem", "")):
            return sends
        # V2 M3 DeepSolver：深思考信号时强制扇出 B 路径（第二候选）
        from utils.confidence_gate import wants_deep_solver
        if wants_deep_solver(
                state.get("question_mode", "computation"),
                state.get("category_confidence"),
                state.get("category", ""),
                state.get("difficulty", "")):
            sends.append(Send("reasoning_agent",
                              {**state, "branch_hint": hint, "method_variant": "B"}))
        elif _want_diverse_branch(state, config):
            sends.append(Send("reasoning_agent",
                              {**state, "branch_hint": hint, "method_variant": "B"}))
        return sends
    sub.add_conditional_edges("solver_decision", fan_out_reasoning, ["reasoning_agent"])
    sub.add_edge("reasoning_agent", "cross_validator")
    sub.add_edge("cross_validator", END)
    return sub.compile()
