"""客观题独立盲复核节点（移植 ICMA objective_review，2026-09-08）。

"第二位独立阅卷教师"：不看第一候选的答案，只凭原题与学科口径重新判断。
目的：打破 Intern 系模型在判断题/选择题上的系统性偏向——单一候选的偏向
没有第二样本对照就无从发现。两候选一致才高置信出厂；不一致交语义仲裁
二选一；复核失败/预算不足时返回空候选，下游完全退化为旧版单候选路径。

仅客观题（choice/true_false/fill）触发；单次 prefill 调用（抑制私有推理）
约 35s/4096 token，是整条客观题路径上唯一新增的调用。
"""
import re

from utils.deps import get_deps
from utils.llm_retry_guard import chat_prefilled
from utils.score_budget import estimate_tokens
from utils.domain_excerpt import select_skill_excerpt
from utils.question_profile import (
    fill_answer_matches_blanks,
    is_objective_mode,
    normalize_objective_answer,
    objective_answer_is_usable,
)
from config import CONFIG


_ANSWER_LINE_RE = re.compile(r"答案\s*[：:]\s*(.+?)(?:\n|$)")


def _parse_review_answer(response, question_mode, problem):
    """从复核响应里提炼答案行并归一化。失败返回 ''。

    优先级：显式"答案："标签行（取最后一个）→ 题型退化恢复
    （choice 行尾字母串 / true_false 首行 / fill 尾部多行拼接）。
    任何候选都必须通过 usable + 填空空位数检查。
    """
    raw = response if isinstance(response, str) else str(response or "")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    candidates = []
    for line in reversed(lines):
        m = _ANSWER_LINE_RE.search(line)
        if m:
            candidates.append(m.group(1).strip())
            break
    if not candidates and lines:
        if question_mode == "choice":
            candidates.append(lines[-1])
        elif question_mode == "true_false":
            candidates.append(lines[0])
        elif question_mode == "fill":
            candidates.append("；".join(lines[-3:]))
    for candidate in candidates:
        normalized = normalize_objective_answer(candidate, question_mode)
        if not objective_answer_is_usable(normalized, question_mode):
            continue
        if question_mode == "fill" and not fill_answer_matches_blanks(normalized, problem):
            continue
        return normalized
    return ""


def objective_review_step(state, config):
    deps = get_deps(config)
    cfg = CONFIG.get("objective_review", {}) or {}
    question_mode = state.get("question_mode", "computation")
    empty = {"objective_review_result": {"answer": ""}, "objective_review_trace": []}
    if not cfg.get("enabled", True) or not is_objective_mode(question_mode):
        return empty
    # 时间紧张时跳过复核：它是可选证据源，压哨阶段绝不挤占收尾链的时限。
    if deps.time_budget and deps.time_budget.fast_path():
        empty["objective_review_trace"] = [{"status": "skipped_fast_path"}]
        return empty

    problem = state.get("problem", "")
    category = state.get("category", "")
    skill_doc = ""
    if deps.skills_loader:
        skill_doc = deps.skills_loader.get_skill_document(category)
    skill_reference = select_skill_excerpt(skill_doc, problem, 2200)

    prompt = (
        "你是独立的第二位阅卷教师。请只根据下面原题与学科口径参考重新判断答案，"
        "不要参考任何先前候选、题库解答或外部答案。逐项核对题面定义；"
        "选择题检查每个选项，判断题检查命题方向，填空题按空位顺序覆盖全部结果。"
        "作答口径以下方'学科口径参考'为准。\n\n"
        f"题型：{question_mode}\n学科：{category}\n"
        f"学科口径参考（教材判分口径，可信）：\n{skill_reference or '（无）'}\n"
        f"原题：\n{problem}"
    )
    trace = []
    try:
        response = chat_prefilled(
            deps.client,
            messages=[{"role": "user", "content": prompt}],
            prefix="答案：",
            temperature=float(cfg.get("temperature", 0.2)),
            max_tokens=int(cfg.get("max_tokens", 4096)),
            logger=deps.logger,
            time_budget=deps.time_budget,
            expected_call_seconds=int(cfg.get("expected_call_seconds", 35)),
            label="objective_review",
        )
        if deps.token_budget:
            deps.token_budget.consume(estimate_tokens(prompt), estimate_tokens(response))
    except Exception as exc:  # noqa: BLE001 - 复核是可选证据源，失败降级单候选路径
        deps.logger.warning("Objective review failed: %s", exc)
        trace.append({"status": "failed", "error": str(exc)[:200]})
        return {"objective_review_result": {"answer": ""},
                "objective_review_trace": trace}

    answer = _parse_review_answer(response, question_mode, problem)
    trace.append({"status": "ok" if answer else "unparsed",
                  "response_chars": len(response or "")})
    return {"objective_review_result": {"answer": answer},
            "objective_review_trace": trace}
