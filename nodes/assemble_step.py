from utils.deps import get_deps
from utils.answer_sanitize import extract_partial_findings, is_junk
from utils.response_builder import (
    attach_steps,
    build_proof_body,
    format_answer_for_output,
    post_process_final_response,
    _clean_noise_head,
)
from utils.verdict_salvage import recover_verdict_sentence
from utils.cot_trim import is_placeholder_answer, strip_cot_prefix
from utils.answer_pull import looks_incomplete_answer
from utils.llm_retry_guard import chat_prefilled, chat_with_retry
from utils.prompt_bank import COORDINATOR_PROMPT, PROOF_COORDINATOR_PROMPT
from utils.score_budget import estimate_tokens
from config import CONFIG
from utils.question_profile import is_objective_mode


def _evidence_override(state: dict, validated: str) -> tuple[str, str]:
    """证据优先的最终闸门（最高优先级）。

    从架构上杜绝此类事故：当 Python 证据（evidence=contradict）明确反驳的
    正是即将输出的推理答案、且 Python 自己持有一个不同的、可用的、非伪造的
    答案时，无论上游（仲裁超时/跳过）留下了什么，最终输出都必须切换到携带
    证据的一方。返回 (最终答案, 覆盖说明)；不满足条件时原样返回。
    """
    po = state.get("python_output") or {}
    rr = state.get("reasoning_result") or {}
    if not validated:
        return validated, ""
    if po.get("evidence_status") != "contradict":
        return validated, ""
    # FAIL 是验证器对其**自身输出**的明确否定。它可以说明待核验的推理
    # 候选有问题，却绝不能让同一份已失败的 Python 输出反客为主成为最终答案。
    if po.get("verification_marker") == "FAIL":
        return validated, "python_self_verification_failed"
    if (po.get("authenticity") or {}).get("fabricated"):
        return validated, ""
    python_answer = str(po.get("answer") or "").strip()
    reasoning_answer = str(rr.get("answer") or "").strip()
    if not python_answer or is_placeholder_answer(python_answer) \
            or looks_incomplete_answer(python_answer) or is_junk(python_answer):
        return validated, ""

    import re
    def _key(text):
        return re.sub(r"[\s。．.,，;；]+", "", text)
    # 只有当被输出的就是被反驳的推理候选、且 Python 给出的是另一个答案时才覆盖。
    if _key(validated) != _key(reasoning_answer) or _key(python_answer) == _key(validated):
        return validated, ""
    return python_answer, "evidence_priority_override"


#: 部分分析末尾展示的字符数。结论位于推导的结尾，因此尾部才是信息丰富的部分。
_PARTIAL_TAIL_CHARS = 400

_PARTIAL_HEADER = "未能完成完整推导，以下为已获得的部分结果：\n"


def _with_emergency_answer(guess: str, partial: str, source: str) -> str:
    """应急直答在前；已获部分结论改用中性标题附后（避免与直答自相矛盾）。"""
    final = f"最终答案：{guess}"
    if source != "generic_error":
        supplement = partial.replace(_PARTIAL_HEADER, "求解过程中获得的部分结论：\n", 1)
        final += f"\n\n{supplement}"
    return final


def _partial_response(state: dict) -> tuple[str, str]:
    """无答案且无时间时的最优可得文本。

    旧实现最后两级直接倾倒原始推理尾部（"哪里出错了？啊！"、"s_j." 等大量
    碎片皆出于此）。现在每一级都过洁净度门，并新增"结构化部分结论"与
    stdout 挖掘两级；原始尾部只有在通过噪声检测时才可输出，否则宁可给通用
    失败说明也不给不可判分噪声。
    """
    from utils.output_miner import mine_stdout_answer

    rr = state.get("reasoning_result") or {}
    python_output = state.get("python_output") or {}
    raw_resp = (state.get("reasoning_raw_response") or "").strip()
    analysis = (rr.get("analysis") or "").strip()
    python_answer = str(python_output.get("answer") or "").strip()
    stdout = str(python_output.get("stdout") or "")
    for source, candidate in (
        ("reasoning_conclusion", recover_verdict_sentence(analysis)),
        ("reasoning_conclusion", recover_verdict_sentence(raw_resp)),
        # Python 侧答案（含 stdout 二次抽取的 answer_source=stdout_mined）：
        # 截断前打印的结论是真实计算产物，优于任何推理残段。
        ("python_answer", python_answer),
        ("python_stdout_mined", mine_stdout_answer(stdout)),
        # 结构化部分结论：从推理文本中收集已证引理/已确认断言，替代原始思维流。
        ("partial_findings", extract_partial_findings(analysis)),
        ("partial_findings", extract_partial_findings(raw_resp)),
        # 尾部仅在通过噪声门时可用（此类碎片全部拦在这道门外）。
        ("reasoning_tail", analysis[-_PARTIAL_TAIL_CHARS:]),
    ):
        text = (candidate or "").strip()
        if text and not is_placeholder_answer(text) and not is_junk(text):
            return f"{_PARTIAL_HEADER}{text}", source
    return "解题过程中出现错误，无法给出完整答案。", "generic_error"


def _emergency_direct_answer(state: dict, deps) -> str:
    """双分支全灭时的最后一搏：动用 safeguard 做一次种子直答（~30s）。

    曾出现多题双分支 900s 全灭后以 generic_error 收尾。与其交白卷，不如用
    剩余硬时限发一次 prefill 直答——助手种子抑制私有推理，模型基于题面与已获
    线索给出最可信结论。失败或产出噪声时返回 ""。
    """
    clock = deps.time_budget
    quota = CONFIG.get("emergency_safeguard_s", 90)
    if clock and clock.remaining_hard() < quota:
        return ""
    rr = state.get("reasoning_result") or {}
    clues = []
    salvaged = recover_verdict_sentence(rr.get("analysis", ""))
    if salvaged:
        clues.append(salvaged)
    findings = extract_partial_findings(rr.get("analysis", ""), limit_chars=600)
    if findings:
        clues.append(findings)
    mined = str((state.get("python_output") or {}).get("answer") or "").strip()
    if mined:
        clues.append(f"程序计算线索：{mined}")
    clue_block = ""
    if clues:
        clue_block = "\n已获得的部分线索（可信度有限）：\n" + "\n".join(clues)[:2200]
    prompt = (
        "下面这道数学题此前的求解未能完成。请你根据题目（和已有线索）直接给出最可能的"
        "最终答案：单行、具体（数值/表达式/集合/结论），不要解释过程，不要说无法确定。\n\n"
        f"题目：\n{state.get('problem', '')[:6000]}\n{clue_block}"
    )
    try:
        raw = chat_prefilled(
            deps.client,
            messages=[{"role": "user", "content": prompt}],
            prefix="最终答案：",
            temperature=0.3,
            max_tokens=CONFIG["max_tokens"].get("emergency_answer", 1280),
            logger=deps.logger,
            time_budget=clock,
            expected_call_seconds=30,
            label="emergency_answer",
            safeguard_margin_s=45,
        )
    except Exception as exc:  # noqa: BLE001 - emergency path never raises.
        deps.logger.warning("Emergency direct answer failed: %s", exc)
        return ""
    if deps.token_budget:
        deps.token_budget.consume(estimate_tokens(prompt), estimate_tokens(raw))
    text = strip_cot_prefix(raw or "")
    import re
    match = re.search(r"最终答案\s*[：:]\s*(.+)", text, re.DOTALL)
    answer = (match.group(1) if match else text).strip()
    answer = answer.splitlines()[0].strip() if answer else ""
    if not answer or len(answer) > 300 or is_placeholder_answer(answer) \
            or is_junk(answer) or looks_incomplete_answer(answer):
        return ""
    return answer


def assemble_step(state, config):
    deps = get_deps(config)
    client = deps.client
    budget = deps.token_budget
    rr = state.get("reasoning_result") or {}
    validated = state.get("validated_answer") or rr.get("answer", "")
    if is_placeholder_answer(validated):
        validated = ""
    ptype = (state.get("validation_details") or {}).get("problem_type", "computation")
    if is_objective_mode(state.get("question_mode", ptype)) and validated:
        # 客观题路径已返回规范、简短的答案。再一次叙事生成无法提升正确性，
        # 反而可能丢掉选项字母或多个空位中的某一空，因此原样保留。
        # 但仍需经过洁净化（剥标签、去前缀），确保最终答案干净可判分。
        cleaned = format_answer_for_output(validated, ptype)
        prefix = "最终答案："
        final = cleaned if cleaned.lstrip().startswith(prefix) else prefix + cleaned
        return {"final_response": final, "coordination_detail": "",
                "fallback_source": "objective_validated_answer"}
    # 证据优先最终闸门：被自家验证器反驳的答案不得出厂。
    # 仲裁明确锁定的选择（answer_locked）尊重仲裁；未锁定的候选一律过闸。
    # 曾出现仲裁已读完双方证据仍选择推理分支（selected_unresolved，因交叉验证
    # "矛盾未决"无法锁定），协调器闸门随后用 Python 分支的错误答案覆盖了仲裁的
    # 显式选择——闸门是为"仲裁超时/跳过"设计的，仲裁在场时不得推翻它。
    evidence_note = ""
    if not state.get("answer_locked") \
            and state.get("arbiter_decision") != "reasoning":
        validated, evidence_note = _evidence_override(state, validated)
        # 洁净度终门：交叉验证在双分支皆弱时可能放行一段探索散文；deadline 路径
        # 会把它原样出厂。修复优先于丢弃——"1/2 works." 这类正确值+口癖要剥尾保值，
        # 修不出干净头部才置空。仲裁锁定的选择不经此门（已过仲裁自己的噪声门），
        # 证明题的结论体裁不同也不经此门。
        if validated and ptype != "proof" and is_junk(validated):
            repaired = _clean_noise_head(validated)
            deps.logger.warning(
                "Validated answer failed the cleanliness gate; %s: %.80s",
                "kept clean head" if repaired else "dropping", validated,
            )
            validated = repaired
    if state.get("answer_locked") and validated:
        # 语义仲裁器逐字选中了某个既有候选。不要再让另一个 LLM 或格式化器
        # 改写/压缩所选文本。但仍需经过洁净化（剥标签、去前缀），
        # 确保最终答案干净可判分。
        cleaned = format_answer_for_output(validated, ptype)
        prefix = "结论：" if ptype == "proof" else "最终答案："
        final = cleaned if cleaned.lstrip().startswith(prefix) else prefix + cleaned
        # 追加推导概要不触碰被选中的答案文本，逐字保证仍然成立。仅当被选中者是
        # 推理分支时才附加——Python 分支的答案不是这些步骤推导的对象。
        if state.get("arbiter_decision") == "reasoning":
            if ptype == "proof":
                # 证明题按"结论+必要过程"判分：关键蕴含链必须写入 final_response
                # 而非仅存 trace（否则会因缺少过程丢分）。
                final = build_proof_body(final, rr)
            else:
                final = attach_steps(final, state.get("problem", ""), rr.get("steps"))
        return {"final_response": final, "coordination_detail": "",
                "fallback_source": "validated_answer"}

    # Deadline 守卫：协调器会写整段叙事说明，这是图中第二长的生成。它只是
    # *重排*我们已经持有的答案，因此时间紧张时直接输出该答案，而不是冒
    # 最终一无所有的风险。证明题只要还有时间就保留叙事，因为对它们而言
    # 推导本身就是交付物。
    time_budget = deps.time_budget
    out_of_time = bool(time_budget) and (
        time_budget.expired() or (ptype != "proof" and time_budget.fast_path()))
    if out_of_time and validated:
        # 时间紧时直接输出已持答案，但同样经过洁净化（剥标签、去前缀）。
        cleaned = format_answer_for_output(validated, ptype)
        prefix = "结论：" if ptype == "proof" else "最终答案："
        final = cleaned if cleaned.lstrip().startswith(prefix) else prefix + cleaned
        # 概要来自我们已经持有的状态，因此不花 LLM 时间，降级路径上仍负担得起。
        if ptype == "proof":
            final = build_proof_body(final, rr)
        else:
            final = attach_steps(final, state.get("problem", ""), rr.get("steps"))
        deps.logger.warning(
            "Coordinator skipped under time budget (%.0fs left)", time_budget.remaining()
        )
        return {"final_response": final, "coordination_detail": "",
                "fallback_source": evidence_note or "validated_answer"}
    if out_of_time:
        # 既无答案也无时间。协调器无法凭空发明它没被给的答案——但硬 safeguard 上
        # 的一次种子应急调用仍能产出可信的具体结论（generic_error 与碎片输出
        # 均为必失分，直答的期望严格更高）。
        deps.logger.warning(
            "Coordinator has no answer and no budget (%.0fs left); trying emergency answer",
            time_budget.remaining())
        partial, source = _partial_response(state)
        guess = _emergency_direct_answer(state, deps)
        if guess:
            return {"final_response": _with_emergency_answer(guess, partial, source),
                    "coordination_detail": "",
                    "fallback_source": "emergency_direct_answer"}
        return {"final_response": partial, "coordination_detail": "",
                "fallback_source": source}

    problem_type_label = {
        "proof": "证明题",
        "choice": "选择题",
        "true_false": "判断题",
        "fill": "填空题",
    }.get(ptype, "计算题")
    steps_fmt = "\n".join(f"步骤{s.get('step_num')}: {s.get('description', '')}" for s in rr.get("steps", []))
    prompt = COORDINATOR_PROMPT.format(
        problem=state["problem"], category=state.get("category", ""),
        problem_type_label=problem_type_label,
        reasoning_steps_formatted=steps_fmt,
        reasoning_analysis=rr.get("analysis", ""),
        python_code=state.get("python_code", ""),
        python_output=(state.get("python_output") or {}).get("stdout", ""),
        validation_status=state.get("validation_status", ""),
        validated_answer=validated)
    from utils.answer_schema import coverage_report
    import json
    prompt += "\n题面交付覆盖检查（缺项需补齐，不确定项需语义核对）：\n" + json.dumps(
        coverage_report(state["problem"], validated), ensure_ascii=False)
    if ptype == "proof":
        from utils.proof_review import candidate_proof_text, targeted_proof_checks
        requested_checks = targeted_proof_checks(
            state["problem"], state.get("category", ""), candidate_proof_text(state))
        from utils.proof_claims import indexed_handoff, compact_index
        handoff = indexed_handoff(state, [task["id"] for task in requested_checks["tasks"]])
        prompt_handoff = {**handoff, "candidate_index": compact_index(handoff["candidate_index"])}
        prompt = PROOF_COORDINATOR_PROMPT.format(
            problem=state["problem"], category=state.get("category", ""),
            validation_status=state.get("validation_status", ""),
            validated_answer=validated, proof_handoff=json.dumps(prompt_handoff, ensure_ascii=False, separators=(',', ':')),
            targeted_checks=json.dumps(requested_checks, ensure_ascii=False))
    try:
        raw = chat_with_retry(
            client,
            messages=[{"role": "user", "content": prompt}],
            temperature=CONFIG["temperatures"]["coordinator"],
            max_tokens=CONFIG["max_tokens"]["coordinator"],
            logger=deps.logger,
            time_budget=time_budget,
            label="coordinator",
        )
    except Exception as exc:  # noqa: BLE001 - never lose an answer we already hold.
        deps.logger.warning("Coordinator LLM unavailable (%s); emitting validated answer", exc)
        if not validated:
            # 直接抛出异常会丢弃已有的部分工作，并让错误兜底打印通用失败串。
            # 改为输出部分证据——它严格更有信息量，且不会更失分。
            partial, source = _partial_response(state)
            guess = _emergency_direct_answer(state, deps)
            if guess:
                return {"final_response": _with_emergency_answer(guess, partial, source),
                        "coordination_detail": "",
                        "fallback_source": "emergency_direct_answer"}
            return {"final_response": partial, "coordination_detail": "",
                    "fallback_source": source}
        prefix = "结论：" if ptype == "proof" else "最终答案："
        final = validated if validated.lstrip().startswith(prefix) else prefix + validated
        if ptype == "proof":
            final = build_proof_body(final, rr)
        return {"final_response": final, "coordination_detail": "",
                "fallback_source": evidence_note or "validated_answer"}
    if budget:
        budget.consume(estimate_tokens(prompt), estimate_tokens(raw))
    cleaned_raw = strip_cot_prefix(raw)
    proof_review = {"status": "unknown", "mathematical_correctness_verified": False}
    if ptype == "proof":
        from utils.proof_review import extract_review
        cleaned_raw, proof_review = extract_review(cleaned_raw)
        proof_review["finish_reason"] = getattr(raw, "finish_reason", None)
        proof_review["requested_checks"] = requested_checks
        from utils.proof_claims import claim_coverage
        proof_review["candidate_index"] = handoff["candidate_index"]
        proof_review["claim_coverage"] = claim_coverage(
            proof_review, handoff["candidate_index"], getattr(raw, "finish_reason", None))
        if proof_review["claim_coverage"]["status"] == "uncertain" and proof_review["status"] in {"none_reported", "issues_corrected"}:
            proof_review.setdefault("model_reported_status", proof_review["status"])
            proof_review["status"] = "unresolved"
    final = post_process_final_response(cleaned_raw, validated, ptype, problem=state["problem"])
    if not isinstance(final, str) or not final.strip():
        final = f"最终答案：{validated}" if validated else "无法生成完整答案。"
    # 证明题答案本身已包含完整推导；只有需要过程的计算题才追加概要。
    if ptype != "proof":
        final = attach_steps(final, state.get("problem", ""), rr.get("steps"))
    # coordination_detail 保留完整解题说明，供 trace 记录（计算题 final_response 仅含简洁答案）
    return {"final_response": final, "coordination_detail": cleaned_raw,
            **({"proof_review": proof_review} if ptype == "proof" else {}),
            "fallback_source": evidence_note or "coordinator_llm"}
