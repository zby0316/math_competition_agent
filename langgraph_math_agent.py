from langgraph.graph import StateGraph, START, END
from state.math_agent_state import MathAgentState
from graph.solving_subgraph import build_solving_subgraph
from nodes import (
    input_node, classifier_node, reconciliation_node, semantic_arbiter_node,
    coordinator_node, critic_node, playoff_node, substitute_node,
    answer_vote_node,
)
from utils.error_handler import node_wrapper

def build_math_agent_graph():
    """VeritasMath 主图。

    相对基线架构的拓扑升级：
      match/锁定 ─┐                              ┌─► coordinator（原"match 直达"）
                  ├─► critic（过程审计，必经）───┤
      arbiter ────┘                              └─► reconciliation（带定向修复提示）
      mismatch ─► playoff（确定性复算裁决）─► critic / reconciliation / arbiter
      match(medium/hard 计算题) ─► substitute（单候选代回核验，V2.6.1）
                                  └─► PASS→critic / FAIL→reconciliation

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
    g.add_node("substitute", node_wrapper(substitute_node, "substitute"))
    g.add_node("critic", node_wrapper(critic_node, "critic"))
    g.add_node("coordinator", node_wrapper(coordinator_node, "coordinator"))
    # V2.7.1 投票节点（外图）：match 出厂前 与 语义仲裁选定后 的第三证据。
    # 主流路径实证（16-cal 9/16、hard12 10/12 走 uncertain_arbitrating）：
    # 大多数题由仲裁单选，故投票必须跟在仲裁之后才真正生效。
    g.add_node("answer_vote", node_wrapper(answer_vote_node, "answer_vote"))

    g.add_edge(START, "input")
    g.add_edge("input", "classifier")
    g.add_edge("classifier", "solving")

    def route_after_solving(state, config):
        if state.get("should_terminate", False):
            return "critic"
        requested = state.get("next_node")
        if requested == "playoff":
            return "playoff"
        # V2.6.1：match 的 medium/hard 计算题先进代回核验，再进 Critic。
        if requested == "substitute":
            return "substitute"
        if requested in {"reconciliation", "semantic_arbiter"}:
            return requested
        # match / 默认：先过 V2.7 快速采样投票（第三证据），再进 Critic
        return "answer_vote"
    g.add_conditional_edges("solving", route_after_solving,
                            {"critic": "critic", "reconciliation": "reconciliation",
                             "semantic_arbiter": "semantic_arbiter", "playoff": "playoff",
                             "substitute": "substitute", "answer_vote": "answer_vote"})

    def route_after_substitute(state, config):
        # FAIL → 带定向修复提示回调解重算；PASS/INCONCLUSIVE/跳过 → 投票 → Critic。
        if state.get("next_node") == "reconciliation":
            return "reconciliation"
        return "answer_vote"
    g.add_conditional_edges("substitute", route_after_substitute,
                            {"reconciliation": "reconciliation", "answer_vote": "answer_vote"})

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
        # 仲裁选定/兜底后先进 V2.7 快速采样投票（对仲裁答案的第三证据），
        # 再按投票结果决定：反证 → 仲裁重选（一次），否则 → Critic 审计门。
        return "answer_vote"
    g.add_conditional_edges("semantic_arbiter", route_after_semantic_arbiter,
                            {"reconciliation": "reconciliation", "answer_vote": "answer_vote"})

    def route_after_answer_vote(state, config):
        # 投票反证（本轮次内）→ 票选赢家已进候选池，仲裁重选一次；
        # 轮次守卫已耗尽的 contested 不会走到这里（节点直接返回 {}）。
        if state.get("answer_vote_status") == "contested":
            return "semantic_arbiter"
        return "critic"
    g.add_conditional_edges("answer_vote", route_after_answer_vote,
                            {"semantic_arbiter": "semantic_arbiter", "critic": "critic"})

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
            # 状态键是 "metadata"（不是 "meta"）——错键曾使 idx 恒为 -1、
            # mark_started 的 _started 集合全部坍缩到同一元素（审计 2026-08-21）。
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
        # V3.3 第二 Intern-S 模型复核通道：每题解析一次（配额按题隔离）。
        # 解析失败/不可用 → review_client=None，分歧路径静默回退主 client，
        # 与单模型行为完全一致。绝不外溢异常。
        self.review_client_name = None
        try:
            from utils.review_model import resolve_review_client
            deps.review_client = resolve_review_client(self.client, logger=deps.logger)
            self.review_client_name = getattr(
                deps.review_client, "review_model_name", None)
        except Exception as exc:  # noqa: BLE001 - 复核通道锦上添花，不拖垮单题
            deps.review_client = None
            self.review_client_name = None
            try:
                deps.logger.warning("review client resolution failed: %s", exc)
            except Exception:  # noqa: BLE001
                pass
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
