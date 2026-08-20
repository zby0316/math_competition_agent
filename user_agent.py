# -*- coding: utf-8 -*-
"""平台入口（VeritasMath 多智能体图架构）。

平台契约：
    from user_agent import ReasoningAgent
    agent = ReasoningAgent(client=official_client)
    result = agent.solve(problem, metadata)   # -> {"final_response": str, "trace": list}

0 分防线（移植自启元 MathAgent 真机血泪教训，逐条有实证）：
1. sys.path 自举——平台用 importlib 按路径加载本文件而不把仓库目录加入
   sys.path 时，多模块 import 必 ImportError；自举必须在 exec 无 __file__
   的沙箱加载器下也零异常（__file__/CWD 双候选定位）。
2. 核心加载失败兜底——任一内部模块导入失败时，fallback ReasoningAgent 仍能
   用 client.chat 直调出真答案，不白丢分。
3. solve 签名全兼容——平台 runner 实测可能不传 metadata（无默认值 → TypeError
   → 全批 0 分）；本入口 metadata=None 默认 + *args/**kwargs 容忍 + __call__/run 别名。
4. client 响应归一化——官方 client 可能返回 choices dict / content blocks /
   bytes / 对象形态（见 utils/response_normalize.py），在 llm_retry 单点收口。
"""

from __future__ import annotations

import os as _os
import sys as _sys
from typing import Any, Dict

# ── 防线 1：sys.path 自举（任何加载形态不抛异常）──
_HERE = ""
try:
    _HERE = _os.path.dirname(_os.path.abspath(__file__))
except Exception:  # noqa: BLE001 - exec/沙箱加载器可能不提供 __file__
    _HERE = ""
for _cand in (_HERE, _os.getcwd()):
    try:
        if (_cand and _os.path.isfile(_os.path.join(_cand, "langgraph_math_agent.py"))
                and _cand not in _sys.path):
            _sys.path.insert(0, _cand)
    except Exception:  # noqa: BLE001
        pass

try:
    from langgraph_math_agent import MathAgentGraph
    from utils.client_tuning import cap_internal_retries, raise_socket_timeout
    from utils.client_probe import probe_signature, probe_contract, read_chat_mode
    from utils.skills_loader import SkillsLoader
    from utils.python_mcp_client import PythonMCPClient
    from utils.token_budget import TokenBudget
    from utils.logger import get_logger
    from state.math_agent_state import create_initial_state
    _BACKEND = "graph"
    _IMPORT_ERROR = ""
except Exception as _exc:  # noqa: BLE001 - 核心加载失败的最后防线
    MathAgentGraph = None  # type: ignore[assignment]
    _BACKEND = "fallback"
    _IMPORT_ERROR = repr(_exc)[:200]

# ==================== PARTICIPANT DESIGN AREA START ====================


if _BACKEND == "graph":

    class ReasoningAgent:
        """挑战杯数学推理智能体（LangGraph 多代理架构）。"""

        # 官方 client 契约探测结果缓存：首次 solve 时各发一次最小调用探测
        # 返回形态/finish_reason/prefill，之后 112 题复用，不再重复探测。
        _contract_probe_cache = None

        def __init__(self, client, *args, **kwargs):
            self.client = client
            self.logger = get_logger("ReasoningAgent")
            # A 120s socket timeout severs healthy reasoning calls on hard problems
            # (measured: 3x120s exhausted, branch lost). Best-effort raise; the platform
            # keeps ownership of the client, model and rate limiting.
            self.transport_tuning = {
                "socket_timeout": raise_socket_timeout(client, logger=self.logger),
                "internal_retries": cap_internal_retries(client, logger=self.logger),
            }
            # 零成本探针：inspect.signature 看官方 client 的 chat 到底接受哪些参数，
            # 直接回答"max_tokens/temperature 传不传得进去"（V2.4/本次 truncated 根因）。
            self.client_probe = probe_signature(client)
            self.logger.info(
                "client probe: type=%s signature=%s max_tokens=%s",
                self.client_probe.get("client_type"),
                self.client_probe.get("chat_signature"),
                self.client_probe.get("accepts_max_tokens"),
            )
            self.skills_loader = SkillsLoader()
            self.mcp_client = PythonMCPClient()
            self.graph = MathAgentGraph(
                client=client, skills_loader=self.skills_loader, mcp_client=self.mcp_client)
            self.logger.info("ReasoningAgent initialized")

        def solve(self, problem: str, metadata: dict = None, *args, **kwargs) -> dict:
            # Normalise at the boundary, inside nothing that can raise. This is the
            # platform's only entry point, so an exception escaping here forfeits the
            # whole problem — while every layer inside the graph already degrades to a
            # real answer. `metadata.get(...)` used to run ahead of the try block, so a
            # non-dict metadata raised straight through.
            if metadata is None and args and isinstance(args[0], dict):
                metadata = args[0]
            meta = metadata if isinstance(metadata, dict) else {}
            idx = meta.get("idx", -1)
            try:
                self.logger.info(f"Solving problem {idx}")
                # 低成本契约探测：首次 solve 时各发一次最小调用（返回形态 +
                # finish_reason + prefill），结果类级缓存，112 题只探测一次。
                if ReasoningAgent._contract_probe_cache is None:
                    ReasoningAgent._contract_probe_cache = probe_contract(self.client)
                    self.logger.info(
                        "client contract probe: %s",
                        ReasoningAgent._contract_probe_cache)
                self.client_probe["contract"] = ReasoningAgent._contract_probe_cache
                initial = create_initial_state(str(problem or ""), meta)
                final_state = self.graph.run(initial, token_budget=TokenBudget())
                result = {
                    "final_response": final_state.get("final_response", "最终答案：未能求解"),
                    "trace": self._build_trace(final_state),
                }
                self._validate_output(result)
                return result
            except Exception as e:
                self.logger.error(f"Error solving problem {idx}: {e}")
                return {
                    "final_response": "最终答案：未能求解",
                    "trace": [{"step": "error", "content": str(e), "idx": idx}],
                }

        def __call__(self, problem, *args, **kwargs):  # 调用方式防御
            return self.solve(problem, *args, **kwargs)

        def run(self, problem, *args, **kwargs):  # 调用方式防御
            return self.solve(problem, *args, **kwargs)

        def _build_trace(self, state: dict) -> list:
            trace = []
            if state.get("category"):
                trace.append({
                    "step": "classification",
                    "category": state.get("category"),
                    "question_mode": state.get("question_mode", "computation"),
                    "confidence": state.get("category_confidence", 0.0),
                    "candidates": state.get("candidate_categories", []),
                    "difficulty": state.get("difficulty", ""),
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
                        # 评审证据（2026-08-10 评委建议 9）：答案来源（四章节解析 /
                        # 显式标记回退 / 散文捞回 / 压缩重试）与原始响应节选，让
                        # "模型答错"与"解析器漏掉"在 trace 里可区分。
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
            # VeritasMath：第二推理路径（异构方法）审计证据
            if state.get("reasoning_result_alt"):
                alt = state["reasoning_result_alt"] or {}
                trace.append({
                    "step": "diverse_reasoning",
                    "answer": alt.get("answer", ""),
                    "thinking": {
                        "purpose": "异构方法（构造/枚举/不变量/反向）独立第二解法，打破单样本系统性偏差。",
                        "analysis": self._truncate(alt.get("analysis", ""), 1200),
                        "steps_count": len(alt.get("steps", [])),
                    },
                    "tools": [
                        {
                            "name": "reasoning_llm_diverse",
                            "attempts": state.get("reasoning_trace_alt", []),
                        }
                    ],
                })
            # VeritasMath：确定性复算季后赛裁决证据
            if state.get("playoff_trace"):
                trace.append({
                    "step": "deterministic_playoff",
                    "status": state.get("playoff_status", ""),
                    "answer": state.get("playoff_answer", ""),
                    "thinking": {
                        "purpose": "候选冲突时不重解题、只做代回复算裁决：方程回代/目标值实测/小规模枚举对照。",
                    },
                    "tools": [
                        {
                            "name": "playoff_executor",
                            "rounds": state.get("playoff_trace", []),
                        }
                    ],
                })
            # V2.6.1：单候选代回核验证据（match 后 medium/hard 计算题的第三证据）
            if state.get("substitution_trace"):
                trace.append({
                    "step": "substitution_check",
                    "status": state.get("substitution_status", ""),
                    "thinking": {
                        "purpose": "match 后把已一致答案代回题面条件做确定性核验，戳穿两路同源同错。",
                    },
                    "tools": [
                        {
                            "name": "substitution_executor",
                            "rounds": state.get("substitution_trace", []),
                        }
                    ],
                })
            # VeritasMath：过程审计（题面契约完整性 + 关键计算抽核）证据
            if state.get("critic_trace"):
                trace.append({
                    "step": "process_audit",
                    "status": state.get("critic_status", ""),
                    "missing": state.get("critic_missing", []),
                    "thinking": {
                        "purpose": "定稿前审计答案是否覆盖题面全部交付物，并抽核关键中间计算。",
                    },
                    "tools": [
                        {
                            "name": "critic_llm",
                            "rounds": state.get("critic_trace", []),
                        }
                    ],
                })
            if state.get("objective_revote_trace"):
                trace.append({
                    "step": "objective_revote",
                    "thinking": {
                        "purpose": "客观题低置信形态触发独立复核投票，防止单采样定生死。",
                    },
                    "tools": [
                        {
                            "name": "objective_revote_llm",
                            "rounds": state.get("objective_revote_trace", []),
                        }
                    ],
                })
            if state.get("modular_guard", {}) and state.get("modular_guard", {}).get("hit"):
                trace.append({
                    "step": "modular_guard",
                    "thinking": {
                        "purpose": "F_2/Z_m 语境的结构内聚合强制条款与静态核查。",
                        "cues": state.get("modular_guard", {}).get("cues", []),
                    },
                })
            if state.get("semantic_arbiter_trace"):
                trace.append({
                    "step": "semantic_arbitration",
                    "status": state.get("semantic_arbiter_status", ""),
                    "decision": state.get("semantic_arbiter_decision", ""),
                    "answer_locked": state.get("answer_locked", False),
                    "thinking": {
                        "purpose": "在不依赖题型关键词的情况下，从既有答案候选中选择完整正确答案。",
                        "policy": "select_existing_candidate_or_abstain",
                    },
                    "tools": [
                        {
                            "name": "semantic_arbiter_llm",
                            "attempts": state.get("semantic_arbiter_attempts", 0),
                            "rounds": state.get("semantic_arbiter_trace", []),
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
            # Stage-level timings: the 2026-07-29 judge report had to *estimate* where
            # its 1307s went because nothing recorded per-stage cost. Now it is measured.
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
            # 官方 client 契约探针：签名（__init__ 零成本）+ 契约（首题低成本）+
            # 主流程实际落到哪种调用模式（跑完后读 chat_compatible 的缓存）。
            # 用于根治 V2.4/本次 truncated_count 的根因（max_tokens 是否丢失 /
            # prefill 是否抑制 CoT / finish_reason 是否可见）。
            try:
                trace.append({
                    "step": "client_probe",
                    "thinking": {
                        "purpose": "探测官方注入 client 的真实契约（签名/返回形态/finish_reason/prefill），定位截断根因。",
                    },
                    "tools": [
                        {
                            "name": "client_probe",
                            "signature": getattr(self, "client_probe", {}),
                            "chat_mode": read_chat_mode(self.client),
                        }
                    ],
                })
            except Exception:  # noqa: BLE001 - 探针失败不影响 trace 主体
                pass
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


else:

    # ── 防线 2 的自包含实现（不 import 任何项目模块——核心图加载失败时
    #    utils.* 同样不可达，必须自包含，否则兜底形同虚设）──
    def _fb_blocks_text(blocks):  # noqa: ANN001
        if isinstance(blocks, str):
            return blocks
        if isinstance(blocks, bytes):
            try:
                return blocks.decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                return ""
        if not isinstance(blocks, list):
            return ""
        parts = []
        for b in blocks:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, bytes):
                try:
                    parts.append(b.decode("utf-8", "ignore"))
                except Exception:  # noqa: BLE001
                    pass
            elif isinstance(b, dict):
                t = b.get("text") or b.get("content") or ""
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts)

    def _fb_normalize(resp):  # noqa: ANN001
        """官方 client 全形态响应 → 纯文本（自包含版）。"""
        if resp is None:
            return ""
        if isinstance(resp, str):
            return resp
        if isinstance(resp, bytes):
            try:
                return resp.decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                return ""
        if isinstance(resp, list):
            return _fb_blocks_text(resp)
        if isinstance(resp, dict):
            try:
                ch = resp.get("choices") or []
                if ch and isinstance(ch[0], dict):
                    msg = ch[0].get("message") or ch[0]
                    _t = _fb_blocks_text(msg.get("content"))
                    if _t:
                        return _t
            except Exception:  # noqa: BLE001
                pass
            for k in ("content", "text", "answer"):
                _t = _fb_blocks_text(resp.get(k))
                if isinstance(_t, str) and _t.strip():
                    return _t
            return ""
        c = getattr(resp, "content", None)
        _t = _fb_blocks_text(c)
        if _t:
            return _t
        try:
            return str(resp)
        except Exception:  # noqa: BLE001
            return ""

    def _fb_chat(client, messages, temperature, max_tokens):  # noqa: ANN001
        """签名探测三级降级（自包含版）。"""
        try:
            return client.chat(messages=messages, temperature=temperature,
                               max_tokens=max_tokens)
        except TypeError:
            try:
                return client.chat(messages, temperature, max_tokens)
            except TypeError:
                return client.chat(messages)

    class ReasoningAgent:  # noqa: D101 - 防线 2：核心加载失败也能出真答案
        """核心图模块加载失败时的兜底 Agent：client.chat 直调，不白丢分。"""

        def __init__(self, client=None, *args, **kwargs):
            self.client = client

        def solve(self, problem, metadata=None, *args, **kwargs):  # noqa: ANN001
            if metadata is None and args and isinstance(args[0], dict):
                metadata = args[0]
            meta = metadata if isinstance(metadata, dict) else {}
            idx = meta.get("idx", -1)
            final = ""
            try:
                if self.client is not None and hasattr(self.client, "chat"):
                    raw = _fb_chat(
                        self.client,
                        [{"role": "user", "content": str(problem or "")}],
                        0.2, 8192)
                    final = _fb_normalize(raw).strip()
            except Exception:  # noqa: BLE001
                final = ""
            return {
                "final_response": final or "最终答案：未能求解",
                "trace": [{"step": "fallback_backend",
                           "content": f"graph import failed: {_IMPORT_ERROR[:120]}",
                           "idx": idx}],
            }

        def __call__(self, problem, *args, **kwargs):  # noqa: ANN001
            return self.solve(problem, *args, **kwargs)

        def run(self, problem, *args, **kwargs):  # noqa: ANN001
            return self.solve(problem, *args, **kwargs)


# ===================== PARTICIPANT DESIGN AREA END =====================
