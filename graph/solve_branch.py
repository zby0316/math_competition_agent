from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from state.agent_state import AgentState
from nodes import reason_step, verify_step, judge_step, objective_review_step
from utils.node_guard import node_wrapper
from utils.question_profile import is_objective_mode

#: 证明题跳过 Python 分支（False = 回退 71 分原版行为）。
#: match_proof_answer 在 Python 失败时置信 0.70 < 门槛 0.8 → 必为 uncertain →
#: python_answer 不进候选 → 证明题无论 Python 成败对最终答案几乎无影响，却消耗
#: 16384 token + 550s。省下的时间回流推理续写与仲裁。
PROOF_SKIP_PYTHON = True

def build_solve_branch():
    sub = StateGraph(AgentState)
    sub.add_node("reasoning_agent", node_wrapper(reason_step, "reasoning_agent"))
    sub.add_node("objective_review", node_wrapper(objective_review_step, "objective_review"))
    sub.add_node("python_agent", node_wrapper(verify_step, "python_agent"))
    sub.add_node("cross_validator", node_wrapper(judge_step, "cross_validator"))

    def fan_out(state, config):
        reasoning_send = Send(
            "reasoning_agent", {**state, "branch_hint": state.get("reasoning_retry_hint")}
        )
        if is_objective_mode(state.get("question_mode", "computation")):
            # 客观题由推理节点自行完成简洁校验，不再扇出第二条昂贵的
            # LLM/代码分支做交叉验证。
            return [reasoning_send]
        if (state.get("question_mode") == "proof" and PROOF_SKIP_PYTHON):
            return [reasoning_send]
        return [
            reasoning_send,
            Send("python_agent", {**state, "branch_hint": state.get("python_retry_hint")}),
        ]
    sub.add_conditional_edges(START, fan_out, ["reasoning_agent", "python_agent"])
    # 客观题：推理 → 独立盲复核 → 交叉验证（复核是第二样本，与推理答案
    # 在 cross_validator 汇合比对）；其余题型：推理/Python 直入交叉验证。
    def route_after_reasoning(state, config):
        if is_objective_mode(state.get("question_mode", "computation")):
            return "objective_review"
        return "cross_validator"
    sub.add_conditional_edges("reasoning_agent", route_after_reasoning,
                              {"objective_review": "objective_review",
                               "cross_validator": "cross_validator"})
    sub.add_edge("objective_review", "cross_validator")
    sub.add_edge("python_agent", "cross_validator")
    sub.add_edge("cross_validator", END)
    return sub.compile()
