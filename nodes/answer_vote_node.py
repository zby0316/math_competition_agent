# -*- coding: utf-8 -*-
"""V2.7 自适应快速采样投票（answer_vote）。

动机（历史评测日志 9 轮 53-62 分 + 失败模式实证）：
- 官方 judger 是 Intern-S 语义判分，单次采样定生死是最大噪声源；
- 已有 sample_vote 只在 hard/低置信触发且依赖完整四章节输出（贵）；本节点在
  cross_validator 判定 match 之后、出厂之前，用 **prefill 答案先行** 的独立
  短采样（~10-20s/次）做"第三证据"：多数确认 → 白捡置信；多数反证 → 把
  票选赢家作为第 4 个候选交给语义仲裁（跨模型独立法官），把"两路一致地错"
  在出厂前拦下。
- N 按难度 + 剩余预算 + API 实测速度自适应：慢 API 自动降 N，绝不牺牲完成率
  （PaperPacer 铁律：6h 内答完 > 单题完美）。
- 客观题不参与（已有 revote/judge_confirm 双向确认）；确定性答案
  （sympy_pre_solve/det_solver）不参与（符号计算高于一切采样）；
  证明/推导/解释题不参与（答案不是标量，投票无意义）。
- 复核模型可用时采样走第二 Intern-S 模型（V3.3 通道，跨模型独立性更强），
  配额尽/不可用自动回退主模型。
"""

from __future__ import annotations

import re

from config import CONFIG
from utils.cot_stripper import is_placeholder_answer
from utils.deps import get_deps
from utils.problem_profile import is_objective_mode
from utils.review_model import chat_prefilled_review

#: 不参与投票的题型（question_mode 全集 {proof, computation, choice,
#: true_false, fill}；proof 答案不是可比标量，客观题另有 revote/judge_confirm）
_NON_VOTABLE_MODES = {"proof"}
#: 确定性答案来源（符号计算高于一切采样，不参与投票）
_DET_SOURCES = {"sympy_pre_solve", "det_solver"}
#: 单样本最大答案长度（防长文本）
_MAX_ANSWER_CHARS = 400
#: 数值比对相对容差
_NUM_TOL = 1e-6
#: 数学内容信号：投票样本必须是可比的标量/表达式，纯散文（模型没按契约
#: 只输出答案、或测试桩噪声）不得参与投票——否则"mock response"这类垃圾
#: 会聚类成假反证，把每题都推进仲裁。
_MATHY_RE = re.compile(r"[0-9=<>+\-*/^\\_{}\[\]]|[√π∞]")
_VERDICT_WORDS = ("无解", "不存在", "发散", "无穷", "不收敛", "无极限",
                  "不可数", "不可约", "互素", "不可导", "不连续")


def _looks_mathy(answer: str) -> bool:
    if _MATHY_RE.search(answer):
        return True
    return any(word in answer for word in _VERDICT_WORDS)

_FAST_SAMPLE_PROMPT = (
    "你是数学老师。请独立解答下面这道计算题（不要参考任何已有答案，"
    "从题面条件重新推导）。只输出最终答案本身，不要输出推导过程、"
    "不要解释。\n\n题目：\n{problem}\n\n最终答案："
)


def _extract_answer(raw: str) -> str:
    """从 prefill 采样产物中提取答案本体（stitch 后带前缀）。"""
    text = str(raw or "")
    text = re.sub(r"^最终答案\s*[：:]?\s*", "", text.strip())
    first_line = text.splitlines()[0].strip() if text else ""
    answer = first_line.strip("`*$ \t。．.,，;；")
    if not answer or is_placeholder_answer(answer):
        return ""
    return answer[:_MAX_ANSWER_CHARS]


def _as_float(text: str):
    """答案是否可解析为单个数值（含科学计数法/分数/负号）。"""
    candidate = re.sub(r"[，,;；\s]+", "", str(text or "").strip())
    try:
        return float(candidate)
    except ValueError:
        try:
            from fractions import Fraction
            return float(Fraction(candidate))
        except Exception:  # noqa: BLE001
            return None


def _answers_agree(a: str, b: str) -> bool:
    """两个答案是否本质相同：数值按相对容差，否则归一化字符串比对。"""
    if not a or not b:
        return False
    fa, fb = _as_float(a), _as_float(b)
    if fa is not None and fb is not None:
        scale = max(abs(fa), abs(fb), 1.0)
        return abs(fa - fb) <= _NUM_TOL * scale
    ka = re.sub(r"[\s。．.,，;；:：]+", "", str(a))
    kb = re.sub(r"[\s。．.,，;；:：]+", "", str(b))
    return bool(ka) and ka == kb


def _vote_n(state: dict, deps) -> int:
    """自适应采样数：medium 2 / hard 4，按预算与 API 速度下调，0=不投。"""
    base = int((CONFIG.get("answer_vote_n") or {}).get(
        str(state.get("difficulty") or "").lower(), 0))
    if base <= 0:
        return 0
    clock = deps.time_budget
    if clock is None:
        return base
    estimate = float(CONFIG.get("answer_vote_estimate_s", 20))
    # 观察到的单次 LLM 实测耗时（spend_log 均价），慢则减 N
    observed = 0.0
    try:
        spend = clock.spend_log() or []
        costs = [float(item.get("seconds") or item.get("elapsed") or 0)
                 for item in spend if item.get("seconds") or item.get("elapsed")]
        if costs:
            observed = sum(costs) / len(costs)
    except Exception:  # noqa: BLE001
        observed = 0.0
    per_sample = max(estimate, observed * 1.2) if observed else estimate
    # 投票+仲裁兜底后必须留出 coordinator/critic 的 reserve
    reserve = float(CONFIG.get("time_reserve_s", 300))
    n = base
    while n > 1:
        cost = n * per_sample + 60
        if clock.remaining_hard() - reserve >= cost:
            break
        n -= 1
    if clock.remaining_hard() - reserve < per_sample + 60:
        return 0
    return n


def answer_vote_node(state, config):
    """出厂前第三证据：多数确认或反证。返回状态更新或 {}。

    V2.7.1 重定位：真机回归实证（16-cal 9/16、本地基准 10/12 走
    uncertain_arbitrating）——主流路径根本不是 match 而是"匹配器判不了
    → 仲裁单选"。本节点因此移到**外图仲裁之后**，对两类答案投票：
      ① 交叉验证 match 的答案（原路径）；
      ② 语义仲裁刚选定的答案（selected/selected_unresolved，主流路径）。
    仲裁选定 = answer_locked 不再阻止投票（那正是要复核的对象）；
    循环防护用 answer_vote_round（每题的投票轮次上限，默认 1 轮）：
    投票反证 → 仲裁重选（票选赢家进候选池）→ 再投票被轮次闸门挡住。
    """
    if not CONFIG.get("enable_answer_vote", True):
        return {}
    # 轮次守卫：每题至多投 answer_vote_max_rounds 轮（防投票-仲裁循环）。
    # 轮次耗尽时若上一轮是"反证"（仲裁已据此重选过一次），必须把状态
    # 改为 contested_final——否则 route_after_answer_vote 会因状态仍是
    # contested 无限循环回仲裁。
    _round = int(state.get("answer_vote_round", 0) or 0)
    if _round >= int(CONFIG.get("answer_vote_max_rounds", 1)):
        if state.get("answer_vote_status") == "contested":
            return {"answer_vote_status": "contested_final"}
        return {}
    question_mode = state.get("question_mode", "computation")
    if is_objective_mode(question_mode) or question_mode in _NON_VOTABLE_MODES:
        return {}
    answer_source = (state.get("reasoning_result") or {}).get("answer_source", "")
    if answer_source in _DET_SOURCES:
        return {}
    validated = str(state.get("validated_answer") or "").strip()
    if not validated or is_placeholder_answer(validated):
        return {}
    if len(validated) > _MAX_ANSWER_CHARS:
        return {}
    # 触发条件：match（出厂前）或语义仲裁已选定（主流路径）。
    status = state.get("validation_status", "")
    arb_status = state.get("semantic_arbiter_status", "")
    if status != "match" and arb_status not in {"selected", "selected_unresolved"}:
        return {}
    deps = get_deps(config)
    n = _vote_n(state, deps)
    if n <= 0:
        return {
            "answer_vote_status": "skipped",
            "answer_vote_trace": [{"status": "skipped", "reason": "budget"}],
            "answer_vote_round": _round + 1,
        }
    # 请求硬上限余量检查（24 次/题封顶；投票不值得挤掉出厂路径）
    clock = deps.time_budget
    if clock is not None:
        try:
            cap = int(CONFIG.get("max_llm_calls_per_problem", 24) or 0)
            used = clock.llm_call_count() if callable(
                getattr(clock, "llm_call_count", None)) else 0
            if cap > 0 and used + n > cap:
                n = max(0, cap - used)
                if n <= 0:
                    return {
                        "answer_vote_status": "skipped",
                        "answer_vote_trace": [{"status": "skipped",
                                               "reason": "request_cap"}],
                        "answer_vote_round": _round + 1,
                    }
        except Exception:  # noqa: BLE001
            pass
    problem = str(state.get("problem") or "")
    temperatures = list(CONFIG.get("sample_temperatures") or [0.4, 0.8, 1.0])
    samples = []
    trace = []
    for i in range(n):
        temperature = temperatures[i % len(temperatures)]
        try:
            raw = chat_prefilled_review(
                deps,
                messages=[{"role": "user", "content":
                           _FAST_SAMPLE_PROMPT.format(problem=problem)}],
                prefix="最终答案：",
                temperature=temperature,
                max_tokens=int(CONFIG.get("answer_vote_max_tokens", 512)),
                logger=deps.logger,
                time_budget=clock,
                expected_call_seconds=float(CONFIG.get("answer_vote_estimate_s", 20)),
                label="answer_vote",
            )
        except Exception as exc:  # noqa: BLE001 - 单样本失败不阻断
            trace.append({"round": i, "status": "error",
                          "error": f"{type(exc).__name__}"[:80]})
            continue
        answer = _extract_answer(raw)
        if not answer or not _looks_mathy(answer):
            trace.append({"round": i, "status": "unusable",
                          "raw": str(raw or "")[:80]})
            continue
        agree = _answers_agree(answer, validated)
        samples.append(answer)
        trace.append({"round": i, "answer": answer[:200],
                      "agree": agree, "temperature": temperature})
    usable = [s for s in samples if s]
    if not usable:
        return {"answer_vote_status": "insufficient",
                "answer_vote_trace": trace,
                "answer_vote_round": _round + 1}
    disagree = [s for s in usable if not _answers_agree(s, validated)]
    if not disagree:
        details = dict(state.get("validation_details") or {})
        details["answer_vote"] = {"status": "all_agree", "n": len(usable)}
        return {
            "answer_vote_status": "all_agree",
            "answer_vote_trace": trace,
            "answer_vote_round": _round + 1,
            "validation_details": details,
        }
    # 反对票内部聚类：≥ 半数可用样本指向同一反方答案 → 交仲裁（带第 4 候选）
    from collections import Counter
    cluster = Counter()
    for s in disagree:
        found_key = next((k for k in cluster if _answers_agree(k, s)), None)
        cluster[found_key or s] += 1
    winner, votes = cluster.most_common(1)[0]
    threshold = max(2, len(usable) // 2)
    if votes < threshold:
        return {"answer_vote_status": "scattered",
                "answer_vote_trace": trace,
                "answer_vote_round": _round + 1}
    details = dict(state.get("validation_details") or {})
    details["answer_vote"] = {
        "status": "contested", "n": len(usable), "winner_votes": votes,
        "winner": winner[:200],
    }
    details["routing_reason"] = "answer_vote_contested_semantic_arbitration"
    return {
        "answer_vote_status": "contested",
        "answer_vote_trace": trace,
        "answer_vote_result": {"winner": winner, "votes": votes},
        "answer_vote_round": _round + 1,
        "validation_status": "vote_contested",
        "validation_details": details,
        "next_node": "semantic_arbiter",
    }
