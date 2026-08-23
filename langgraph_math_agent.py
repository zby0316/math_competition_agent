from langgraph.graph import StateGraph, START, END
from state.math_agent_state import MathAgentState
from graph.solving_subgraph import build_solving_subgraph
from nodes import (
    input_node, classifier_node, reconciliation_node, semantic_arbiter_node,
    coordinator_node, critic_node, playoff_node,
)
from utils.error_handler import node_wrapper

def build_math_agent_graph():
    """VeritasMath 主图。

    相对基线架构的拓扑升级：
      match/锁定 ─┐                              ┌─► coordinator（原"match 直达"）
                  ├─► critic（过程审计，必经）───┤
      arbiter ────┘                              └─► reconciliation（带定向修复提示）
      mismatch ─► playoff（确定性复算裁决）─► critic / reconciliation / arbiter

    所有进入 coordinator 的答案都必须先过 Critic 审计门——这是"题面契约完整性"
    在架构上的强制保证，而不是依赖提示词自觉。
    """
    g = StateGraph(MathAgentState)
    g.add_node("input", node_wrapper(input_node, "input"))
    g.add_node("classifier", node_wrapper(classifier_node, "classifier"))
    g.add_node("solving", build_solving_subgraph())
    g.add_node("reconciliation", node_wrapper(reconciliation_node, "reconciliation"))
    g.add_node("semantic_arbiter", node_wrapper(semantic_arbiter_node, "semantic_arbiter"))
    g.add_node("playoff", node_wrapper(playoff_node, "playoff"))
    g.add_node("critic", node_wrapper(critic_node, "critic"))
    g.add_node("coordinator", node_wrapper(coordinator_node, "coordinator"))

    g.add_edge(START, "input")
    g.add_edge("input", "classifier")
    g.add_edge("classifier", "solving")

    def route_after_solving(state, config):
        if state.get("should_terminate", False):
            return "critic"
        requested = state.get("next_node")
        if requested == "playoff":
            return "playoff"
        if requested in {"reconciliation", "semantic_arbiter"}:
            return requested
        # match / 默认：原直达 coordinator，现必经 Critic 过程审计门
        return "critic"
    g.add_conditional_edges("solving", route_after_solving,
                            {"critic": "critic", "reconciliation": "reconciliation",
                             "semantic_arbiter": "semantic_arbiter", "playoff": "playoff"})

    def route_after_reconciliation(state, config):
        requested = state.get("next_node")
        if requested in {"solving", "semantic_arbiter", "coordinator"}:
            return requested
        return "coordinator"
    g.add_conditional_edges("reconciliation", route_after_reconciliation,
                            {"solving": "solving", "semantic_arbiter": "semantic_arbiter",
                             "coordinator": "coordinator"})

    def route_after_semantic_arbiter(state, config):
        if state.get("next_node") == "reconciliation":
            return "reconciliation"
        # 仲裁选定/兜底后同样过审计门（仲裁只看候选间相对完整性，Critic 看题面契约）
        return "critic"
    g.add_conditional_edges("semantic_arbiter", route_after_semantic_arbiter,
                            {"reconciliation": "reconciliation", "critic": "critic"})

    def route_after_playoff(state, config):
        requested = state.get("next_node")
        if requested in {"critic", "reconciliation", "semantic_arbiter"}:
            return requested
        return "semantic_arbiter"
    g.add_conditional_edges("playoff", route_after_playoff,
                            {"critic": "critic", "reconciliation": "reconciliation",
                             "semantic_arbiter": "semantic_arbiter"})

    def route_after_critic(state, config):
        return "reconciliation" if state.get("next_node") == "reconciliation" else "coordinator"
    g.add_conditional_edges("critic", route_after_critic,
                            {"reconciliation": "reconciliation", "coordinator": "coordinator"})

    g.add_edge("coordinator", END)
    return g.compile()

class MathAgentGraph:
    def __init__(self, client, skills_loader=None, mcp_client=None):
        self.app = build_math_agent_graph()
        self.client = client
        self.skills_loader = skills_loader
        self.mcp_client = mcp_client

    def run(self, initial_state, token_budget=None, time_budget=None):
        from utils.deps import Deps
        from utils.paper_pacer import PaperPacer
        from utils.token_budget import TokenBudget
        from utils.time_budget import TimeBudget
        tb = token_budget or TokenBudget()
        # One clock per problem, started here so every node measures against the
        # same origin. The platform's limit is wall-clock, so this is the budget
        # that actually decides pass/fail.
        clock = time_budget or TimeBudget()
        # V2 M1 全卷完成率引擎：按"剩余全卷时间 ÷ 剩余题数"收紧本
        # 题软预算（paper_cap），保证 6h 内 112 题全部完成。只影响软预算
        # （可选阶段购买力），remaining_hard（1200s 平台硬限）不动。
        try:
            pacer = PaperPacer.get_instance()
            idx = -1
            meta = initial_state.get("metadata") if isinstance(initial_state, dict) else None
            if isinstance(meta, dict):
                idx = meta.get("idx", -1)
            paper_cap = pacer.budget_for(idx)
            pacer.mark_started(idx)
            clock.paper_cap = paper_cap
            # 与难度画像取 min：两者都是"可选阶段购买力"上限
            if paper_cap < clock.soft_total:
                clock.soft_total = float(paper_cap)
        except Exception:  # noqa: BLE001 - 完成率引擎是锦上添花，失败不拖垮单题
            pass
        deps = Deps(client=self.client, skills_loader=self.skills_loader,
                    mcp_client=self.mcp_client, token_budget=tb, time_budget=clock)
        try:
            final_state = self.app.invoke(initial_state, config={"configurable": {"deps": deps}})
        finally:
            # 每题结束都计数（无论成功失败），驱动全卷节奏
            try:
                PaperPacer.get_instance().mark_done()
            except Exception:  # noqa: BLE001
                pass
        if isinstance(final_state, dict):
            final_state["_time_budget"] = clock.snapshot()
            final_state["_llm_spend_log"] = clock.spend_log()
        return final_state
