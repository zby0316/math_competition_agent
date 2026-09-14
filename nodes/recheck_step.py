"""盲目独立复核：两条分支判不出时，用一次"不看任何既有答案"的独立重解做第三方证据。

设计来源与取舍：
* multiface 的 independent_student：至少一路求解**不看任何参考**，防止检索/首轮结论
  把全部路径锚定到同一个错误上；
* ICMA 的 retry_affordability：是否再买一次调用按**上次实测耗时**与剩余硬时限定价，
  而不是"时间还没用完就再来一轮"；
* 本仓库历史教训（multi-path-vote 回退、awa "4 轮反降"）：**不投票、不堆轮数**——
  复核产出只作为证据参与确定性评分与既有仲裁，绝不直接覆盖候选（"只增不改"）。

触发门限（全部满足才启动）：
1. ``CONFIG["recheck"]["enabled"]`` 为真；
2. 交叉验证状态是 uncertain/mismatch（即判不出的场景——历史上最集中的失败簇）；
3. 至少有一个可用候选，且本题尚未复核过；
4. 硬时限前剩余 ≥ ``min_budget_s``（保证复核后仍付得起仲裁与拼装）。
"""

from __future__ import annotations

from config import CONFIG
from utils.deps import get_deps
from utils.guard_ceiling import NodeTimeoutError, run_with_timeout
from utils.llm_retry_guard import chat_with_retry
from utils.prompt_bank import RECHECK_PROMPT
from utils.score_budget import estimate_tokens

#: 只有这些状态值得复核：判不出来 == 需要第三方证据。
_RECHECKABLE_STATUS = {
    "uncertain_arbitrating",
    "mismatch_arbitrating",
    "reconciliation_exhausted_arbitrating",
}
#: 复核只针对可逐项核对的题型（客观题/证明题走各自既有的低成本路径）。
_RECHECKABLE_MODES = {"computation", "fill"}


def _skip(reason: str) -> dict:
    return {"next_node": "arbiter",
            "recheck_trace": [{"status": "skipped", "reason": reason}]}


def recheck_step(state, config):
    cfg = CONFIG.get("recheck") or {}
    if not cfg.get("enabled", False):
        return _skip("disabled")
    if state.get("recheck_result"):
        # 已复核过：仲裁弃权后的第二轮不再重复付费（时间账不宽裕）。
        return _skip("already_rechecked")
    if (state.get("question_mode") or "computation") not in _RECHECKABLE_MODES:
        return _skip("question_mode_not_recheckable")
    status = state.get("validation_status") or ""
    if status not in _RECHECKABLE_STATUS:
        return _skip(f"status_not_recheckable:{status or 'unknown'}")

    rr = state.get("reasoning_result") or {}
    po = state.get("python_output") or {}
    if not (rr.get("answer") or po.get("answer")):
        return _skip("no_candidate")

    deps = get_deps(config)
    clock = deps.time_budget
    min_budget = float(cfg.get("min_budget_s", 600))
    if clock is not None and clock.remaining_hard() < min_budget:
        return _skip(f"budget:{clock.remaining_hard():.0f}s<{min_budget:.0f}s")

    prompt = RECHECK_PROMPT.format(
        problem=state.get("problem", ""),
        category=state.get("category", "") or "未分类",
    )
    timeout_s = cfg.get("timeout_s", 420)
    try:
        response = run_with_timeout(
            lambda: chat_with_retry(
                deps.client,
                messages=[{"role": "user", "content": prompt}],
                temperature=cfg.get("temperature", 0.7),
                max_tokens=cfg.get("max_tokens", 8192),
                logger=deps.logger,
                time_budget=deps.time_budget,
                label="recheck",
            ),
            timeout_s,
        )
    except NodeTimeoutError:
        deps.logger.warning("Recheck timed out after %ss", timeout_s)
        return _skip(f"timeout:{timeout_s}s")
    except Exception as exc:  # noqa: BLE001 - 复核失败不得影响主流程
        deps.logger.warning("Recheck failed: %s", exc)
        return _skip(f"error:{str(exc)[:120]}")

    # 复用主推理的解析器：它带结论速览/显式标记/散文捞回等抗截断回退，
    # 复核输出同样会被截断，解析口径必须一致。
    from nodes.reason_step import _parse_reasoning_output

    parsed = _parse_reasoning_output(response, question_mode="computation")
    answer = str(parsed.get("answer") or "").strip()
    if deps.token_budget is not None and response:
        deps.token_budget.consume(estimate_tokens(prompt), estimate_tokens(response))
    if not answer:
        return _skip("unparsed_answer")

    return {
        "next_node": "arbiter",
        "recheck_result": {
            "answer": answer,
            "analysis": parsed.get("analysis", ""),
            "steps": parsed.get("steps", []),
            "answer_source": parsed.get("answer_source", "recheck"),
            "raw_excerpt": str(response or "")[:1200],
        },
        "recheck_trace": [{"status": "completed", "answer_len": len(answer),
                           "response_chars": len(response or "")}],
    }
