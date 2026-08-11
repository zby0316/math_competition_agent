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
    - 首轮：只对高风险领域（组合/竞赛构造/微分几何/测度积分，单样本错误率
      最高的四类）随首路径并行发出；
    - 调解重算轮：一律追加（此时已知首路径失败，多样性正是缺的东西）；
    - 客观题、时间 fast path、配置关闭时不发。
    """
    if not CONFIG.get("enable_diverse_reasoning", True):
        return False
    if is_objective_mode(state.get("question_mode", "")):
        return False
    if state.get("reconciliation_round", 0) >= 1:
        return True
    category = state.get("category", "")
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
        if _want_diverse_branch(state, config):
            sends.append(Send(
                "reasoning_agent", {**state, "branch_hint": state.get("reasoning_retry_hint"),
                                    "method_variant": "B"}
            ))
        question_mode = state.get("question_mode", "computation")
        # VeritasMath 治本（ultra_112 idx=13/40）：实算填空题不再跳过 Python 验证——
        # 它们恰是最易算错、最缺机器证据的题型。纯概念填空（术语/定义）才走快速路径。
        if is_objective_mode(question_mode):
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
