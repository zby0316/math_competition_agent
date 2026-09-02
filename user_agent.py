from typing import Any, Dict

from agent_pipeline import AgentPipeline
from config import AGENT_VERSION as CONFIG_AGENT_VERSION
from utils.client_fixups import cap_internal_retries, raise_socket_timeout
from utils.domain_loader import DomainLoader
from utils.exec_client import ExecClient
from utils.score_budget import ScoreBudget
from utils.logging_setup import get_logger
from state.agent_state import create_initial_state

# ==================== 平台入口：solve 契约 ====================


class ReasoningAgent:
    """数学竞赛推理智能体：多阶段流水线求解并产出四章节答案。"""

    def __init__(self, client, *args, **kwargs):
        self.client = client
        self.logger = get_logger("ReasoningAgent")
        # 平台注入的 client 若带 120s socket 超时，会在难题的推理调用上把健康
        # 工作切断（一次调用被 3×120s 全部耗尽即丢分支）。这里尽力调高超时、
        # 掐掉内部重试；client、模型与限流仍归平台所有，改不动也不影响契约。
        self.transport_tuning = {
            "socket_timeout": raise_socket_timeout(client, logger=self.logger),
            "internal_retries": cap_internal_retries(client, logger=self.logger),
        }
        self.skills_loader = DomainLoader()
        self.mcp_client = ExecClient()
        self.graph = AgentPipeline(
            client=client, skills_loader=self.skills_loader, mcp_client=self.mcp_client)
        self.logger.info("ReasoningAgent initialized")
        self.logger.info("Agent version: %s", CONFIG_AGENT_VERSION)

    def solve(self, problem: str, metadata: dict) -> dict:
        # 在边界处归一化，且不放进任何可能抛错的地方。这是平台唯一入口，异常
        # 从这里逃逸等于整题作废——而图内每一层都会降级到真实答案。metadata
        # 可能是任意对象，先归一成 dict 再做 .get，防止非 dict 直接抛错。
        meta = metadata if isinstance(metadata, dict) else {}
        idx = meta.get("idx", -1)
        try:
            self.logger.info(f"Solving problem {idx}")
            initial = create_initial_state(str(problem or ""), meta)
            final_state = self.graph.run(initial, token_budget=ScoreBudget())
            result = {
                "final_response": final_state.get("final_response", "无法生成答案"),
                "trace": self._build_trace(final_state),
            }
            # 版本溯源：trace 头部固定携带 AGENT_VERSION，评测日志的 Commit ID
            # + 该标记双保险，任何一次出分都能对到具体代码改动。
            result["trace"].insert(0, {
                "step": "agent_version",
                "version": CONFIG_AGENT_VERSION,
            })
            self._validate_output(result)
            return result
        except Exception as e:
            self.logger.error(f"Error solving problem {idx}: {e}")
            return {
                "final_response": "解题过程中出现错误，无法给出完整答案。",
                "trace": [{"step": "error", "content": str(e), "idx": idx}],
            }

    def _build_trace(self, state: dict) -> list:
        trace = []
        if state.get("category"):
            trace.append({
                "step": "classification",
                "category": state.get("category"),
                "question_mode": state.get("question_mode", "computation"),
                "confidence": state.get("category_confidence", 0.0),
                "candidates": state.get("candidate_categories", []),
                "tools": [
                    {
                        "name": "category_candidates",
                        "stages": state.get("classification_stages_used", []),
                        "selected": state.get("category"),
                    }
                ],
            })
        if state.get("reasoning_result"):
            rr = state["reasoning_result"]
            trace.append({
                "step": "reasoning",
                "attempts": state.get("reasoning_attempts", 0),
                "answer": rr.get("answer", ""),
                "steps_count": len(rr.get("steps", [])),
                "thinking": {
                    "analysis": rr.get("analysis", ""),
                    "steps": rr.get("steps", []),
                    "final_answer": rr.get("answer", ""),
                    "validation_points": rr.get("validation_points", []),
                    # 答案来源（四章节解析 / 显式标记回退 / 散文捞回 / 压缩
                    # 重试）与原始响应节选，让"模型答错"与"解析器漏掉"在
                    # trace 里可区分。
                    "answer_source": rr.get("answer_source", "parsed_sections"),
                    "raw_response_excerpt": self._excerpt(
                        state.get("reasoning_raw_response", "")),
                },
                "tools": [
                    {
                        "name": "skill_document",
                        "category": state.get("category", ""),
                        "purpose": "加载对应数学领域 skill 文档作为解题参考。",
                    },
                    {
                        "name": "reasoning_llm",
                        "attempts": state.get("reasoning_trace", []),
                        "max_attempts_used": state.get("reasoning_attempts", 0),
                        "output_schema": ["analysis", "steps", "answer", "validation_points"],
                    },
                ],
            })
        if state.get("python_output"):
            po = state["python_output"]
            python_code = state.get("python_code", "")
            trace.append({
                "step": "python_verification",
                "attempts": state.get("python_attempts", 0),
                "success": po.get("success", False),
                "answer": po.get("answer", ""),
                "thinking": {
                    "purpose": "用生成的 Python/SymPy 代码独立复核答案。",
                    "code_summary": self._truncate(python_code, 1600),
                    "expected_output_marker": "最终答案:",
                    "extracted_answer": po.get("answer", ""),
                    "evidence_status": po.get("evidence_status",
                                               state.get("python_evidence_status", "")),
                    "evidence_summary": self._truncate(
                        po.get("evidence_summary",
                               state.get("python_evidence_summary", "")), 1000),
                    "contradictions": state.get("python_contradictions",
                                                 po.get("contradictions", [])),
                },
                "tools": [
                    {
                        "name": "python_code_generation",
                        "attempts": state.get("python_trace", []),
                        "code_length": len(python_code),
                    },
                    {
                        "name": "python_executor",
                        "backend": po.get("execution_backend", ""),
                        "success": po.get("success", False),
                        "execution_time": po.get("execution_time", 0.0),
                        "stdout": self._truncate((po.get("stdout") or "").strip(), 1000),
                        "stderr": self._truncate((po.get("stderr") or "").strip(), 1000),
                    },
                ],
            })
        if state.get("validation_details"):
            trace.append({
                "step": "validation",
                "status": state.get("validation_status"),
                "validated_answer": state.get("validated_answer", ""),
                "thinking": {
                    "problem_type": state["validation_details"].get("problem_type", ""),
                    "reason": state["validation_details"].get("reason", ""),
                    "confidence": state["validation_details"].get("confidence", 0.0),
                    "python_evidence_status": state["validation_details"].get(
                        "python_evidence_status", state.get("python_evidence_status", "")),
                    "python_evidence_summary": self._truncate(
                        state["validation_details"].get(
                            "python_evidence_summary",
                            state.get("python_evidence_summary", "")), 1000),
                    "python_contradictions": state["validation_details"].get(
                        "python_contradictions", state.get("python_contradictions", [])),
                },
                "tools": [
                    {
                        "name": "answer_matcher",
                        "details": state.get("validation_details", {}),
                    }
                ],
            })
        if state.get("validation_history"):
            trace.append({
                "step": "validation_history",
                "count": len(state["validation_history"]),
                "tools": [{
                    "name": "validation_history",
                    "rounds": state["validation_history"],
                }],
            })
        if state.get("problem_anchor") or state.get("problem_integrity_events"):
            anchor = state.get("problem_anchor") or {}
            trace.append({
                "step": "problem_integrity",
                "sha256_prefix": str(anchor.get("sha256", ""))[:16],
                "events": state.get("problem_integrity_events", []),
            })
        if state.get("arbiter_trace"):
            trace.append({
                "step": "arbitration",
                "status": state.get("arbiter_status", ""),
                "decision": state.get("arbiter_decision", ""),
                "answer_locked": state.get("answer_locked", False),
                "thinking": {
                    "purpose": "在不依赖题型关键词的情况下，从既有答案候选中选择完整正确答案。",
                    "policy": "select_existing_candidate_or_abstain",
                },
                "tools": [
                    {
                        "name": "arbiter_llm",
                        "attempts": state.get("arbiter_attempts", 0),
                        "rounds": state.get("arbiter_trace", []),
                    }
                ],
            })
        if state.get("reconciliation_trace"):
            trace.append({
                "step": "reconciliation",
                "count": len(state["reconciliation_trace"]),
                "thinking": {
                    "purpose": "当推理答案与 Python 验证不一致或不确定时，决定是否重跑子图。",
                },
                "tools": [
                    {
                        "name": "reconciliation_policy",
                        "rounds": state["reconciliation_trace"],
                    }
                ],
            })
        if state.get("final_response"):
            trace.append({
                "step": "coordination",
                "content": state.get("coordination_detail", ""),
                "response_length": len(state["final_response"]),
                "thinking": {
                    "purpose": "将推理步骤、验证结果与最终答案整理为面向读者的输出。",
                },
                "tools": [
                    {
                        "name": "coordinator_llm",
                        "content_length": len(state.get("coordination_detail", "")),
                        "skipped_for_locked_answer": bool(state.get("answer_locked")),
                    },
                    {
                        "name": "answer_formatter",
                        "final_response_length": len(state.get("final_response", "")),
                    },
                ],
                "fallback_source": state.get("fallback_source", ""),
            })
        # 各阶段实测耗时与预算消耗随 trace 上报，用于定位超时瓶颈。
        if state.get("node_timings") or state.get("_time_budget"):
            trace.append({
                "step": "timing",
                "thinking": {
                    "purpose": "记录各阶段实测耗时与时间预算消耗，用于定位超时瓶颈。",
                },
                "tools": [
                    {
                        "name": "time_budget",
                        "budget": state.get("_time_budget", {}),
                        "node_elapsed": state.get("node_timings", []),
                        "llm_calls": state.get("_llm_spend_log", []),
                        "transport_tuning": getattr(self, "transport_tuning", {}),
                    }
                ],
            })
        return trace

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        if not isinstance(value, str):
            value = str(value or "")
        if len(value) <= limit:
            return value
        return value[:limit].rstrip() + "...[truncated]"

    @staticmethod
    def _excerpt(value: str, head: int = 600, tail: int = 1400) -> str:
        """头 + 尾节选：截断响应的结论区在尾部，只截头会丢掉最有证据价值的部分。"""
        if not isinstance(value, str):
            value = str(value or "")
        if len(value) <= head + tail + 40:
            return value
        omitted = len(value) - head - tail
        return f"{value[:head]}\n……[中略 {omitted} 字符]……\n{value[-tail:]}"

    def _validate_output(self, result: dict) -> None:
        import json
        assert isinstance(result, dict), "返回值必须是dict"
        assert "final_response" in result, "必须包含final_response"
        assert isinstance(result["final_response"], str), "final_response必须是字符串"
        assert result["final_response"].strip(), "final_response不能为空"
        json.dumps(result, ensure_ascii=False)  # must be JSON-serializable


# ===================== 平台入口：solve 契约 结束 =====================
