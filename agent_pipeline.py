from langgraph.graph import StateGraph, START, END
from state.agent_state import AgentState
from graph.solve_branch import build_solve_branch
from nodes import (
    parse_input, classify_domain, refine_loop, arbiter_step,
    assemble_step,
)
from utils.node_guard import node_wrapper

def build_agent_pipeline():
    g = StateGraph(AgentState)
    g.add_node("input", node_wrapper(parse_input, "input"))
    g.add_node("classifier", node_wrapper(classify_domain, "classifier"))
    g.add_node("solving", build_solve_branch())
    g.add_node("reconciliation", node_wrapper(refine_loop, "reconciliation"))
    g.add_node("arbiter", node_wrapper(arbiter_step, "arbiter"))
    g.add_node("coordinator", node_wrapper(assemble_step, "coordinator"))

    g.add_edge(START, "input")
    g.add_edge("input", "classifier")
    g.add_edge("classifier", "solving")

    def route_after_solving(state, config):
        if state.get("should_terminate", False):
            return "coordinator"
        requested = state.get("next_node")
        if requested in {"reconciliation", "arbiter"}:
            return requested
        return "coordinator"
    g.add_conditional_edges("solving", route_after_solving,
                            {"coordinator": "coordinator", "reconciliation": "reconciliation",
                             "arbiter": "arbiter"})

    def route_after_reconciliation(state, config):
        requested = state.get("next_node")
        if requested in {"solving", "arbiter"}:
            return requested
        return "coordinator"
    g.add_conditional_edges("reconciliation", route_after_reconciliation,
                            {"solving": "solving", "arbiter": "arbiter",
                             "coordinator": "coordinator"})

    def route_after_arbiter(state, config):
        return "reconciliation" if state.get("next_node") == "reconciliation" else "coordinator"
    g.add_conditional_edges("arbiter", route_after_arbiter,
                            {"reconciliation": "reconciliation", "coordinator": "coordinator"})

    g.add_edge("coordinator", END)
    return g.compile()

class AgentPipeline:
    def __init__(self, client, skills_loader=None, mcp_client=None):
        self.app = build_agent_pipeline()
        self.client = client
        self.skills_loader = skills_loader
        self.mcp_client = mcp_client

    def run(self, initial_state, token_budget=None, time_budget=None, client=None):
        from utils.deps import Deps
        from utils.score_budget import ScoreBudget
        from utils.wall_clock import TimeBudget
        tb = token_budget or ScoreBudget()
        # 每题一把时钟，从这里起算，保证所有节点对着同一原点计时。平台限制是
        # 墙钟，因此这份预算是真正决定成败的约束。
        clock = time_budget or TimeBudget()
        deps = Deps(client=client if client is not None else self.client, skills_loader=self.skills_loader,
                    mcp_client=self.mcp_client, token_budget=tb, time_budget=clock)
        final_state = self.app.invoke(initial_state, config={"configurable": {"deps": deps}})
        if isinstance(final_state, dict):
            final_state["_time_budget"] = clock.snapshot()
            final_state["_llm_spend_log"] = clock.spend_log()
        return final_state
