"""Cross-Validator: compare Reasoning vs Python results, route accordingly. §3.5.5.

M4 hardening: for computation problems, prefer the sympy-computed Python answer
when it succeeded (deterministic > LLM prose, which may echo placeholders or
compute wrong values). For proof problems or python-failed cases, use reasoning.

VeritasMath 升级：
1. 三路候选——第二推理路径（reasoning_result_alt）参与比对：主路径与 Python
   不一致但第二路径与 Python 一致时，直接采纳第二路径（异构方法的独立证据）。
2. 计算题 mismatch 先进 deterministic playoff（代回复算裁决），季后赛无法
   裁决时才落入重算/仲裁——把"只能选"升级为"先算清楚再选"。
3. 客观题低置信复核投票：空位覆盖不足等低置信形态触发一次独立短采样比对，
   一致才提置信（评委报告实证：三空答零空曾以 0.78 置信直接放行）。
"""
from utils.answer_matcher import AnswerMatcher
from utils.answer_contract import answer_part_count, missing_components
from utils.answer_extractor import is_multi_part_problem, looks_incomplete_answer
from utils.cot_stripper import is_placeholder_answer
from utils.deps import get_deps
from utils.reconciliation_policy import reconciliation_retry_available
from utils.verification_evidence import parse_verification_evidence
from utils.token_budget import estimate_tokens
from utils.problem_profile import (
    classify_question_mode,
    fill_answer_matches_blanks,
    is_objective_mode,
    normalize_objective_answer,
    objective_answer_is_usable,
)
from config import CONFIG


def _clean_answer(answer: str) -> str:
    return "" if is_placeholder_answer(answer) else (answer or "")


def _same_scalar_or_text(a: str, b: str) -> bool:
    """两个候选是否本质相同（去空白/标点后一致）。"""
    import re as _re
    ka = _re.sub(r"[\s。．.,，;；]+", "", str(a or ""))
    kb = _re.sub(r"[\s。．.,，;；]+", "", str(b or ""))
    return bool(ka) and ka == kb


def _preferred_answer(state: dict, match_result: dict) -> str:
    """Pick the answer to surface as validated_answer.

    证据优先序（2026-08-09 评委建议 1，最高优先级）：
    ``有枚举支撑且反驳对手的 Python 答案 > evidence=support 的分支 > 推理答案 >
    结构化部分结论``。真机实证 事故——Python 枚举出 C(2k,k)² 且 evidence=contradict
    明确反驳了推理的 "4"，仲裁超时后系统仍输出被自家验证器反驳的 "4"——由第一
    条规则在架构上杜绝：证据指向谁，兜底就选谁。

    Computation + python_success + 答案完整 → Python answer (sympy, deterministic).
    Python 答案为空/碎片（评委报告问题 2：'(Matrix([' 等截断片段曾污染
    final_response）→ 回退 reasoning answer；两者都不完整时取非空者兜底。

    uncertain 时保持推理优先（去锚定），但 2026-07-07 评委报告显示：推理侧漏
    结论/漏问项/漏分配而 Python 侧完整时（265/271/283/93/113/275），残缺答案
    仍然胜出。故当推理答案缺契约字段或漏问项、且 Python 答案更完整时改选 Python。
    """
    rr = state.get("reasoning_result") or {}
    po = state.get("python_output") or {}
    problem = state.get("problem", "")
    ptype = match_result.get("problem_type", "computation")
    python_answer = _clean_answer(po.get("answer", ""))
    reasoning_answer = _clean_answer(rr.get("answer", ""))
    python_ok = bool(po.get("success")) and bool(python_answer)
    reasoning_ok = bool(reasoning_answer) and not looks_incomplete_answer(reasoning_answer)
    question_mode = state.get("question_mode") or ""
    if is_objective_mode(question_mode):
        # Keep every degradation path canonical.  In particular, an invalid or
        # unavailable objective candidate may still reach the semantic arbiter's
        # deterministic fallback; returning the raw rationale there would undo the
        # short-path formatting guarantee in coordinator_node.
        for candidate in (reasoning_answer, python_answer):
            normalized = normalize_objective_answer(candidate, question_mode)
            if objective_answer_is_usable(normalized, question_mode):
                return normalized
        return ""
    # 证据优先的最后防线：Python 证据明确反驳推理候选、且 Python 自己持有一个
    # 不同的、可用的、非伪造的答案时，兜底必须选 Python——被自家验证器反驳的
    # 答案不得再被输出。仅在此条件下打破"推理优先"，避免污染无争议路径。
    evidence_status = po.get("evidence_status", "")
    fabricated = bool((po.get("authenticity") or {}).get("fabricated"))
    if evidence_status == "contradict" and python_answer and not fabricated \
            and not looks_incomplete_answer(python_answer) \
            and not _same_scalar_or_text(python_answer, reasoning_answer):
        return python_answer
    if match_result.get("status") == "match" and reasoning_ok:
        # Python/SymPy supplies correctness evidence; retain the already validated
        # human-readable reasoning form for the public answer.
        return reasoning_answer
    if match_result.get("status") == "uncertain" and reasoning_ok:
        if python_ok and not looks_incomplete_answer(python_answer):
            r_missing = missing_components(problem, reasoning_answer)
            p_missing = missing_components(problem, python_answer)
            if len(p_missing) < len(r_missing):
                return python_answer
            if is_multi_part_problem(problem) \
                    and answer_part_count(python_answer) > answer_part_count(reasoning_answer):
                return python_answer
        return reasoning_answer
    if ptype == "computation" and python_ok and not looks_incomplete_answer(python_answer):
        # 反伪造：无实质计算的代码答案不得凭"执行成功"压过一个可用的推理答案。
        if not (fabricated and reasoning_ok):
            return python_answer
    if reasoning_ok:
        return reasoning_answer
    return reasoning_answer or python_answer


def _objective_revote(state: dict, config, current_answer: str, question_mode: str) -> dict:
    """客观题低置信复核投票（VeritasMath）。

    只在首次抽取置信不足时被调用：用同一题型契约做一次**独立**短采样
    （不复用首答的任何上下文，避免锚定），与当前候选比对。
    返回 {"agree": bool, "revote_answer": str, "adopted": str}。
    """
    deps = get_deps(config)
    problem = state.get("problem", "")
    prompt = (
        "你是数学教师。请独立解答下面的客观题，逐项核对题面条件后作答。"
        "不要输出 Thinking Process。只输出两行：\n"
        "答案：<按题型规范作答；填空题多空用分号分隔并写满全部空位>\n"
        "依据：<不超过三句的核对>\n\n"
        f"题型：{question_mode}\n题目：\n{problem}"
    )
    clock = deps.time_budget
    try:
        # V2.5.4 多模型复核：独立复核样本交给第二 Intern-S 模型（跨模型独立
        # 采样 > 同模型重采样）；复核不可用时自动回退主 client。
        from utils.review_model import chat_with_retry_review
        raw = chat_with_retry_review(
            deps,
            messages=[{"role": "user", "content": prompt}],
            temperature=CONFIG["temperatures"].get("objective_reasoning", 0.2),
            max_tokens=CONFIG["max_tokens"]["objective_reasoning"],
            logger=deps.logger,
            time_budget=clock,
            expected_call_seconds=45,
            label="objective_revote",
        )
    except Exception as exc:  # noqa: BLE001 - 复核失败不改变现状
        deps.logger.warning("Objective revote failed: %s", exc)
        return {"agree": None, "revote_answer": "", "adopted": current_answer}
    if deps.token_budget:
        deps.token_budget.consume(estimate_tokens(prompt), estimate_tokens(raw))
    import re as _re
    m = _re.search(r"答案\s*[：:]\s*(.+)", str(raw or ""))
    revote = normalize_objective_answer(m.group(1).strip() if m else "", question_mode)
    if not objective_answer_is_usable(revote, question_mode):
        return {"agree": None, "revote_answer": "", "adopted": current_answer}
    agree = _same_scalar_or_text(revote, current_answer)
    adopted = current_answer
    if not agree:
        # 分歧时保守处理：首选通过空位覆盖检查的一方；都不通过则保留首答。
        # 不直接采纳第二票——两票可信度相同，换票不增加信息，只增加波动。
        revote_complete = question_mode != "fill" or fill_answer_matches_blanks(
            revote, problem)
        current_complete = question_mode != "fill" or fill_answer_matches_blanks(
            current_answer, problem)
        if revote_complete and not current_complete:
            adopted = revote
    return {"agree": agree, "revote_answer": revote, "adopted": adopted}


def cross_validator_node(state, config):
    reasoning_result = state.get("reasoning_result") or {}
    revote_trace = []  # 客观题复核投票留证（仅低置信触发时有内容）
    raw_python_output = state.get("python_output") or {}
    python_output = parse_verification_evidence(
        raw_python_output,
        candidate_answer=reasoning_result.get("answer", ""),
        code=state.get("python_code", ""),
    )
    question_mode = state.get("question_mode") or classify_question_mode(state.get("problem", ""))
    # VeritasMath 治本：实算填空题现在会扇出 Python 验证（verify_router）。
    # 当这类题拿到可用的 Python 候选时，必须走完整 computation 交叉验证
    # （matcher / playoff），而不是客观题快速路径的直接放行——否则 Python
    # 证据被浪费，单采样错答（真机实证）仍会出厂。
    _fill_with_python = (
        question_mode == "fill"
        and bool(str(python_output.get("answer") or "").strip())
        and not python_output.get("skipped")
    )
    if is_objective_mode(question_mode) and not _fill_with_python:
        # Objective questions deliberately have no Python candidate.  A valid,
        # normalized answer from the concise reasoning path is the only candidate
        # worth emitting; sending it through the computation matcher would mark it
        # uncertain solely because Python was skipped and trigger an unnecessary
        # arbitration/reconciliation round.
        candidate = normalize_objective_answer(reasoning_result.get("answer", ""), question_mode)
        if not objective_answer_is_usable(candidate, question_mode):
            candidate = normalize_objective_answer(python_output.get("answer", ""), question_mode)
        if objective_answer_is_usable(candidate, question_mode):
            blank_gap = question_mode == "fill" and not fill_answer_matches_blanks(
                candidate, state.get("problem", ""))
            confidence = 0.45 if blank_gap else 0.78
            reason = (f"客观题快速路径已提取{question_mode}答案"
                      + ("；但分项数少于题面空位数，答案可能不完整" if blank_gap else ""))
            # VeritasMath 复核投票：低置信形态（空位覆盖不足）触发一次独立采样比对。
            # 一致 → 置信提到 0.9；分歧 → 维持 0.45 并保留更完整一方，留证待查。
            # 真机扩展（真机实证）：填空题需实算且单采样易错（符号错/
            # 计数错），即使空位覆盖足也复核一次——独立重解是第二证据，
            # 一致提置信，分歧保留更完整候选。成本 ~45s/题，仅 fill 触发。
            need_revote = (blank_gap or question_mode == "fill")
            if need_revote and CONFIG.get("enable_objective_revote", True):
                clock = getattr(get_deps(config), "time_budget", None)
                # 闸门用硬预算+边际而不是 fast_path：easy 难度画像下软预算
                # 常年处于 fast_path，会把复核投票整体关掉，而它恰恰是为
                # 填空题低覆盖形态设计的。
                affordable = clock is None or (
                    clock.remaining_hard() - 60 >= 45)
                if affordable:
                    vote = _objective_revote(state, config, candidate, question_mode)
                    if vote.get("agree") is True:
                        confidence = 0.9
                        reason += "；复核投票一致"
                    elif vote.get("agree") is False:
                        candidate = vote.get("adopted") or candidate
                        reason += "；复核投票分歧，保留较完整候选"
                    revote_trace.append({
                        "round": state.get("reconciliation_round", 0),
                        "agree": vote.get("agree"),
                        "revote_answer": vote.get("revote_answer", ""),
                    })
            # VeritasMath 移植（启元实证 P0）：判断题双向确认。Intern-S2 对
            # "是否"题存在系统性"否"偏向（启元实测 90% 判断错题同根因），单轮
            # 方向不可靠。确认轮一致才采纳；反向则温度0重解取第三票。
            # 仅 true_false 题型、答案为判断词、预算充足时触发，其余零成本。
            if question_mode == "true_false" and CONFIG.get("enable_judge_confirm", True):
                from utils.judge_confirm import (
                    run_judge_confirmation, should_confirm)
                if should_confirm(state.get("problem", ""), candidate):
                    deps = get_deps(config)

                    def _resolve_prompt():
                        return (f"【题目】{state.get('problem', '')}\n"
                                "请独立判断该命题是否成立：逐步推导后，最后一行"
                                "单独输出 答案:是 或 答案:否（只输出这一行判断）。")

                    jc = run_judge_confirmation(
                        state.get("problem", ""), candidate, deps,
                        main_prompt_builder=_resolve_prompt)
                    if jc.get("action") == "confirm":
                        confidence = min(confidence + 0.12, 0.95)
                        reason += f"；双向确认一致（{jc.get('note', '')}）"
                    elif jc.get("action") == "reverse":
                        candidate = jc.get("final_word") or candidate
                        confidence = max(confidence, 0.6)
                        reason += f"；双向确认反向，改判 {candidate}"
                    revote_trace.append({
                        "round": state.get("reconciliation_round", 0),
                        "judge_confirm": jc.get("action"),
                        "note": jc.get("note", ""),
                    })
            match_result = {
                "status": "match",
                "verdict": True,
                "comparison_verdict": True,
                "confidence": confidence,
                "reason": reason,
                "method": "objective_direct",
                "problem_type": question_mode,
                "text_similarity": 1.0,
                "matched_fields": [],
                "mismatched_fields": [],
                "field_coverage": 0.5 if blank_gap else 1.0,
            }
        else:
            match_result = {
                "status": "uncertain",
                "verdict": None,
                "comparison_verdict": None,
                "confidence": 0.0,
                "reason": "客观题未提取到可提交的答案",
                "method": "objective_unparsed",
                "problem_type": question_mode,
                "text_similarity": 0.0,
                "matched_fields": [],
                "mismatched_fields": [],
                "field_coverage": 0.0,
            }
    else:
        match_result = dict(AnswerMatcher.match_answers(
            problem=state["problem"],
            reasoning_result=reasoning_result,
            python_result=python_output,
        ))
        # VeritasMath 三路交叉验证：主路径与 Python 未达成一致时，若第二推理
        # 路径（异构方法）存在且与 Python 达成一致，直接采纳第二路径——
        # 这是方法论独立的双证据，强于任何单路径。
        alt = state.get("reasoning_result_alt") or {}
        alt_answer = _clean_answer(alt.get("answer", ""))
        if match_result.get("status") != "match" and alt_answer \
                and not looks_incomplete_answer(alt_answer):
            alt_match = dict(AnswerMatcher.match_answers(
                problem=state["problem"],
                reasoning_result=alt,
                python_result=python_output,
            ))
            if alt_match.get("status") == "match":
                alt_match["adopted_source"] = "reasoning_alt"
                alt_match["reason"] = ("主推理路径未通过验证；第二推理路径（异构方法）"
                                       "与程序验证一致：" + str(alt_match.get("reason", "")))
                match_result = alt_match
                # 主候选原位替换，下游 _preferred_answer/仲裁看到的推理候选
                # 即为已通过验证的第二路径（原首路径保留在 reasoning_result，
                # 仲裁候选池仍可看到两者）。
                reasoning_result = {**alt, "answer_source":
                                    alt.get("answer_source", "reasoning_alt")}
    evidence_status = python_output.get("evidence_status", "inconclusive")
    evidence_summary = python_output.get("evidence_summary", "")
    contradictions = list(python_output.get("contradictions") or [])
    match_result.update({
        "python_evidence_status": evidence_status,
        "python_evidence_summary": evidence_summary,
        "python_contradictions": contradictions,
    })
    status = match_result["status"]
    validated_answer = ""
    next_node = "coordinator"
    problem_type = match_result.get("problem_type", "computation")
    adopted_alt_result = None

    history_entry = {
        "round": state.get("reconciliation_round", 0),
        "status": status,
        "evidence_status": evidence_status,
        "evidence_summary": evidence_summary[:1000],
        "contradictions": contradictions[:10],
    }

    # Explicit evidence of a counterexample outranks textual/symbolic agreement.
    # A program can run successfully while proving that the candidate is false.
    if evidence_status == "contradict":
        match_result["routing_reason"] = "python_evidence_contradiction"
        # VeritasMath 真机修复（真机实证）：contradict 证据可能是
        # **假证据**——Python 自身算错（CRT 逆元求错）却自报"反驳了正确的推理"。
        # 原链路直接仲裁/重算，且 coordinator 证据闸门会强行采信 Python 错值。
        # 修复：计算题且有双候选时，先送 playoff 代回复算裁决谁对谁错，
        # 用确定性计算戳破假证据，而不是盲目信 Python 的自报证据。
        already_played = bool(state.get("playoff_trace"))
        if problem_type == "computation" and CONFIG.get("enable_playoff", True) \
                and not already_played:
            from nodes.playoff_node import playoff_candidates
            cand_a, cand_b = playoff_candidates(
                {**state, "reasoning_result": reasoning_result})
            if cand_a and cand_b:
                match_result["routing_reason"] = \
                    "python_evidence_contradiction_deterministic_playoff"
                history_entry["status"] = "contradict_playoff"
                result = {
                    "validation_status": "contradict_playoff",
                    "validation_details": match_result,
                    "validated_answer": validated_answer,
                    "next_node": "playoff",
                    "python_output": python_output,
                    "python_evidence_status": evidence_status,
                    "python_evidence_summary": evidence_summary,
                    "python_contradictions": contradictions,
                    "validation_history": [history_entry],
                }
                if revote_trace:
                    result["objective_revote_trace"] = revote_trace
                return result
        if reconciliation_retry_available(state, config):
            status = "mismatch_reconciling"
            next_node = "reconciliation"
        else:
            status = "mismatch_arbitrating"
            next_node = "semantic_arbiter"
            match_result["unresolved_contradiction"] = True
        history_entry["status"] = status
        return {
            "validation_status": status,
            "validation_details": match_result,
            "validated_answer": validated_answer,
            "next_node": next_node,
            "python_output": python_output,
            "python_evidence_status": evidence_status,
            "python_evidence_summary": evidence_summary,
            "python_contradictions": contradictions,
            "validation_history": [history_entry],
        }

    # V2.5 三路投票补全：A 与 Python 一致（match）时，若 B（异构第二路径）
    # 不同意 A，说明可能"同源一致地错"——A、Python 都是主模型 S2，符号/方向
    # 可能一起错。B 用异构方法（DIVERSE_METHOD_HINT），它的反对票不能忽略。
    # 触发 deterministic playoff 裁决 A vs B（异构），而非 A vs Python（两者
    # 一致无意义）。纯字符串比对，零 LLM 成本。
    _alt_ans = _clean_answer((state.get("reasoning_result_alt") or {}).get("answer", ""))
    _a_ans = _clean_answer(reasoning_result.get("answer", ""))
    if status == "match" and _alt_ans and _a_ans \
            and not looks_incomplete_answer(_alt_ans) \
            and not _same_scalar_or_text(_alt_ans, _a_ans):
        already_played = bool(state.get("playoff_trace"))
        if problem_type == "computation" and CONFIG.get("enable_playoff", True) \
                and not already_played:
            status = "mismatch_playoff"
            match_result["routing_reason"] = "same_source_agree_but_alt_disagrees"
            match_result["playoff_prefer_alt"] = True
            history_entry["status"] = status
            result = {
                "validation_status": status,
                "validation_details": match_result,
                "validated_answer": validated_answer,
                "next_node": "playoff",
                "python_output": python_output,
                "python_evidence_status": evidence_status,
                "python_evidence_summary": evidence_summary,
                "python_contradictions": contradictions,
                "playoff_prefer_alt": True,
                "validation_history": [history_entry],
            }
            if revote_trace:
                result["objective_revote_trace"] = revote_trace
            return result

    if status == "match":
        if problem_type == "computation" and python_output.get("success") \
                and match_result.get("method") in {"symbolic", "structured_symbolic"}:
            match_result["bypass_reason"] = "python_success_and_symbolic_equivalence"
        match_result["routing_reason"] = "validated_match"
        if match_result.get("method") == "objective_direct":
            validated_answer = normalize_objective_answer(
                reasoning_result.get("answer", ""), question_mode
            )
            if not validated_answer:
                validated_answer = normalize_objective_answer(
                    python_output.get("answer", ""), question_mode
                )
        elif match_result.get("adopted_source") == "reasoning_alt":
            validated_answer = _clean_answer(
                (state.get("reasoning_result_alt") or {}).get("answer", ""))
            # 状态一致性：被采纳的是第二路径，其步骤/分析必须随答案一起成为
            # 主推理结果——否则 coordinator 会把"主路径的错误步骤"贴在
            # "第二路径的正确答案"后面，答案与过程自相矛盾。
            adopted_alt_result = reasoning_result
        else:
            validated_answer = _preferred_answer(state, match_result)
        next_node = "coordinator"
    elif status == "mismatch":
        # VeritasMath：计算题冲突先进 deterministic playoff（候选代回复算），
        # 季后赛已跑过/不适用时回落到既有的重算-仲裁通道。
        already_played = bool(state.get("playoff_trace"))
        if problem_type == "computation" and CONFIG.get("enable_playoff", True) \
                and not already_played:
            from nodes.playoff_node import playoff_candidates
            cand_a, cand_b = playoff_candidates({**state, "reasoning_result": reasoning_result})
            if cand_a and cand_b:
                status = "mismatch_playoff"
                match_result["routing_reason"] = "computation_mismatch_deterministic_playoff"
                history_entry["status"] = status
                result = {
                    "validation_status": status,
                    "validation_details": match_result,
                    "validated_answer": validated_answer,
                    "next_node": "playoff",
                    "python_output": python_output,
                    "python_evidence_status": evidence_status,
                    "python_evidence_summary": evidence_summary,
                    "python_contradictions": contradictions,
                    "validation_history": [history_entry],
                }
                if revote_trace:
                    result["objective_revote_trace"] = revote_trace
                return result
        # subgraph-level retry gated by reconciliation_round (NOT per-node attempts —
        # per-node attempts gate the agent's internal format/code retries; the plan
        # §4.2 keeps these separate).
        if reconciliation_retry_available(state, config):
            status = "mismatch_reconciling"
            next_node = "reconciliation"
        else:
            status = "mismatch_arbitrating"
            match_result["routing_reason"] = "mismatch_retry_exhausted_semantic_arbitration"
            next_node = "semantic_arbiter"
    else:  # uncertain
        # Parser/format uncertainty is exactly where keyword contracts are weakest.
        # Let a semantic judge choose between the two existing answers before paying
        # for another full reasoning+Python run. The arbiter may abstain and route
        # back to reconciliation without changing either candidate.
        status = "uncertain_arbitrating"
        match_result["routing_reason"] = "insufficient_equivalence_evidence_semantic_arbitration"
        next_node = "semantic_arbiter"

    history_entry["status"] = status
    result = {
        "validation_status": status,
        "validation_details": match_result,
        "validated_answer": validated_answer,
        "next_node": next_node,
        "python_output": python_output,
        "python_evidence_status": evidence_status,
        "python_evidence_summary": evidence_summary,
        "python_contradictions": contradictions,
        "validation_history": [history_entry],
    }
    if status == "match" and match_result.get("adopted_source") == "reasoning_alt" \
            and adopted_alt_result:
        result["reasoning_result"] = adopted_alt_result
    if status == "match":
        # 新的确定性 match 取代任何历史仲裁锁——否则 Critic 修复回路重算出的
        # 新答案会被 coordinator 的陈旧 answer_locked 跳过洁净度/证据闸门。
        result["answer_locked"] = False
    if revote_trace:
        result["objective_revote_trace"] = revote_trace
    return result
