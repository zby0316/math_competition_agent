from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from state.agent_state import AgentState
from nodes import reason_step, verify_step, judge_step
from utils.node_guard import node_wrapper
from utils.question_profile import is_objective_mode

def build_solve_branch():
    sub = StateGraph(AgentState)
    sub.add_node("reasoning_agent", node_wrapper(reason_step, "reasoning_agent"))
    sub.add_node("python_agent", node_wrapper(verify_step, "python_agent"))
    sub.add_node("cross_validator", node_wrapper(judge_step, "cross_validator"))

    def _det_precheck(state):
        """V2.5 合并：确定性求解器预检（<1s）。命中则把答案写入 state，
        由仲裁候选池与判分兜底使用——确定性 > LLM 散文。"""
        from config import CONFIG
        if not CONFIG.get("enable_det_solver", True):
            return state
        try:
            from utils.det_solver import det_solve
            r = det_solve(state.get("problem", "") or "")
            if r and r.get("answer"):
                return {**state, "det_answer": r["answer"],
                        "det_kind": r.get("kind", ""),
                        "det_pipeline": r.get("pipeline", "")}
        except Exception:
            pass
        return state

    def fan_out(state, config):
        state = _det_precheck(state)
        reasoning_send = Send(
            "reasoning_agent", {**state, "branch_hint": state.get("reasoning_retry_hint")}
        )
        if is_objective_mode(state.get("question_mode", "computation")):
            # 客观题由推理节点自行完成简洁校验，不再扇出第二条昂贵的
            # LLM/代码分支做交叉验证。
            return [reasoning_send]
        return [
            reasoning_send,
            Send("python_agent", {**state, "branch_hint": state.get("python_retry_hint")}),
        ]
    sub.add_conditional_edges(START, fan_out, ["reasoning_agent", "python_agent"])
    sub.add_edge("reasoning_agent", "cross_validator")
    sub.add_edge("python_agent", "cross_validator")
    sub.add_edge("cross_validator", END)
    return sub.compile()
