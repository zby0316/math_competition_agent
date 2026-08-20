"""单候选确定性代回核验节点（V2.6.1 确定性验证前移）。

背景：cross_validator 的 match 只保证"推理与 Python 两路独立计算一致"，
但两路同源（同一模型、同一题面读法）可能**一致地错**。本节点提供第三种
证据：把候选答案**代回**题面条件核验（方程验残差/极值代回比较/计数小规模
枚举/数值题独立重算），用确定性计算戳穿"同源同错"——这是对标榜一
（80.36 分）"sympy 硬校验"缺口的补课（V2.5 主线 2 一直未落地）。

路由（只在 cross_validator match 且 computation + medium/hard + 未核验过时进入）：
  - PASS        → critic（正常收尾，答案不变）
  - FAIL        → reconciliation（携带定向修复提示重算）
  - INCONCLUSIVE/预算不足/异常 → 放行 critic（核验是锦上添花，不阻塞出答案）

成本纪律：≈ 一次 prefill 代码生成（~150s）+ 一次沙箱执行（≤60s），与 playoff
同档定价（软预算可尽，硬上限前必须容得下生成+执行+收尾）。每题至多一次
（substitution_trace 防重入）。
"""

from __future__ import annotations

import re

from config import CONFIG
from nodes.python_agent_node import _extract_code
from utils.cot_stripper import is_placeholder_answer
from utils.deps import get_deps
from utils.llm_retry import chat_prefilled, thinking_mode_flag
from utils.prompt_templates import SUBSTITUTE_PROMPT
from utils.token_budget import estimate_tokens

#: 代回核验代码生成走 prefill 压缩通道（种子 "```python\n" 抑制私有推理），
#: 8192 token 全部用于代码；估时含一次执行时限。
_SUBSTITUTE_GEN_ESTIMATE_S = 150
_SUBSTITUTE_PREFILL = "```python\n"

_VERDICT_RE = re.compile(r"核验结果\s*[:：]\s*(PASS|FAIL|INCONCLUSIVE)", re.IGNORECASE)


def _bounded(value, limit: int) -> str:
    text = value if isinstance(value, str) else str(value or "")
    return text if len(text) <= limit else text[:limit].rstrip() + "\n...[truncated]"


def _parse_verdict(stdout: str) -> str:
    """从执行输出解析核验结论：PASS / FAIL / INCONCLUSIVE。"""
    match = _VERDICT_RE.search(str(stdout or ""))
    return match.group(1).upper() if match else "INCONCLUSIVE"


def substitute_node(state, config):
    trace = list(state.get("substitution_trace") or [])
    if not CONFIG.get("enable_substitution_check", True):
        return {"substitution_status": "skipped", "substitution_trace": trace,
                "next_node": "critic"}

    deps = get_deps(config)
    clock = deps.time_budget
    answer = str(state.get("validated_answer") or "").strip()
    if not answer or is_placeholder_answer(answer):
        trace.append({"status": "skipped", "reason": "no_usable_answer"})
        return {"substitution_status": "skipped", "substitution_trace": trace,
                "next_node": "critic"}

    # 预算闸门（与 playoff 同档：软预算可尽，硬上限前必须容得下生成+执行+收尾）
    if clock and clock.remaining_hard() - CONFIG.get("compressed_reserve_margin_s", 150) \
            < (_SUBSTITUTE_GEN_ESTIMATE_S + CONFIG["node_timeouts"]["python_mcp_execute"]):
        trace.append({"status": "skipped", "reason": "unaffordable"})
        return {"substitution_status": "skipped", "substitution_trace": trace,
                "next_node": "critic"}

    prompt = SUBSTITUTE_PROMPT.format(
        problem=_bounded(state.get("problem", ""), 8000),
        answer=_bounded(answer, 2500),
    )
    try:
        resp = chat_prefilled(
            deps.client,
            messages=[{"role": "user", "content": prompt}],
            prefix=_SUBSTITUTE_PREFILL,
            temperature=CONFIG["temperatures"]["python"],
            max_tokens=CONFIG["max_tokens"]["python_compressed"],
            logger=deps.logger,
            time_budget=clock,
            expected_call_seconds=_SUBSTITUTE_GEN_ESTIMATE_S,
            label="substitute_generate",
            reserve_margin_s=CONFIG.get("compressed_reserve_margin_s", 150),
            thinking_mode=thinking_mode_flag(),
        )
    except Exception as exc:  # noqa: BLE001 - 核验失败不阻塞出答案
        deps.logger.warning("Substitution check generation failed: %s", exc)
        trace.append({"status": "error", "reason": str(exc)[:200]})
        return {"substitution_status": "error", "substitution_trace": trace,
                "next_node": "critic"}
    if deps.token_budget:
        deps.token_budget.consume(estimate_tokens(prompt), estimate_tokens(resp))

    code = _extract_code(resp)
    if not code:
        # 无代码说明模型判断无法机械核验（证明/概念形态）→ 良性放行。
        trace.append({"status": "indecisive", "reason": "no_verification_code"})
        return {"substitution_status": "indecisive", "substitution_trace": trace,
                "next_node": "critic"}
    if deps.mcp_client is None:
        trace.append({"status": "error", "reason": "executor_unavailable"})
        return {"substitution_status": "error", "substitution_trace": trace,
                "next_node": "critic"}

    output = deps.mcp_client.execute(
        code, timeout=CONFIG["node_timeouts"]["python_mcp_execute"])
    stdout = str(output.get("stdout") or "")
    verdict = _parse_verdict(stdout)
    trace.append({
        "status": "executed",
        "verdict": verdict,
        "execute_success": bool(output.get("success")),
        "stdout_excerpt": _bounded(stdout, 1200),
        "stderr_excerpt": _bounded(output.get("stderr") or "", 400),
    })

    if verdict == "PASS":
        return {"substitution_status": "pass", "substitution_trace": trace,
                "next_node": "critic"}
    if verdict == "FAIL":
        # 强信号：两路一致的答案被独立代回核验证伪 → 定向修复重算。
        hint = ("候选答案经独立代回核验未通过（FAIL）。两路此前的一致结论已被"
                "确定性计算证伪，请彻底更换解题方法重新独立推导，逐项复核题面"
                "条件，并在最终答案前用代码验证新结论。")
        return {
            "substitution_status": "fail",
            "substitution_trace": trace,
            "reasoning_retry_hint": hint,
            "python_retry_hint": hint + "\n请用代码独立验证补齐后的结论。",
            "repair_hint_source": "substitution",
            "next_node": "reconciliation",
        }
    return {"substitution_status": "indecisive", "substitution_trace": trace,
            "next_node": "critic"}
