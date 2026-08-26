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


def _second_python_check(state: dict, config, require_playoff: bool = True) -> str | None:
    """V3.4 Python 双验证（set1 round2 实证：#4/#13/#46/#58/#91 的
    "Python 对但仲裁/playoff 未采用" + 防 idx=28 sympy 确定性错）。

    触发条件（全部满足才花第二次代码调用）：
    - 已走过 playoff 且不可决（indecisive/neither/error，非 decisive）；
      V4.1 规则可判定接管路径可放宽（require_playoff=False）：推理侧
      给不出闭式而 Python 闭式自检 PASS 时，直接做第二份独立代码验证。
    - 第一份 Python 执行成功且自检通过（evidence_status == support）
    - 剩余硬时间充足（>300s）
    动作：用"完全不同方法"的 hint 再生成一份独立验证代码，
    两份 Python 结果数学等价 → 确定性采信 Python（跳过 LLM 仲裁）。
    不等价 → 返回 None（维持原链路，不引入新风险）。
    """
    po = state.get("python_output") or {}
    pa = str(po.get("answer") or "").strip()
    if not pa or is_placeholder_answer(pa):
        return None
    if po.get("evidence_status") != "support":
        return None
    if require_playoff and not state.get("playoff_trace"):
        return None  # 还没走过 playoff，不提前升级
    if state.get("playoff_status") == "decisive":
        return None  # 已确定性裁决，无需双验证
    if state.get("second_python_done"):
        return None
    try:
        from utils.deps import get_deps
        clock = get_deps(config).time_budget
        if clock and clock.remaining_hard() < 300:
            return None
    except Exception:  # noqa: BLE001 - 预算取不到时按可负担处理
        pass
    try:
        from nodes.python_agent_node import python_agent_node
        second_state = {
            **state,
            "python_retry_hint": (
                "第一次代码验证与推理结论冲突且复算未能裁决。请用**完全不同的方法/库**"
                "（暴力枚举替代公式、数值模拟替代符号推导、sympy 替代手算等）独立重新"
                "计算本题目标值，最后一行输出最终答案。"),
        }
        out = python_agent_node(second_state, config)
        second = out.get("python_output") or {}
        sa = str(second.get("answer") or "").strip()
        # V5.3-审计修复（逻辑审计 6b3e2e4a H3）：第二代码**无论成败**都要
        # 闩锁——此前仅在成功路径由调用方置 second_python_done，失败返回
        # None 不闩锁，导致 V3.4/V4.1/V4.7 三个入口条件重叠、同一目的最多
        # 连跑 3 份完整 python 代码（每次 200-450s，预算被烧穿）。
        # 返回值加哨兵标记：第二份代码实际已执行（成功或失败）。
        if out.get("python_attempts", 0) >= 1 or second.get("success"):
            state["second_python_done"] = True
            state["second_python_attempted"] = True
        if not sa or is_placeholder_answer(sa):
            return None
        if AnswerMatcher._sympy_equivalent(pa, sa, 1e-6) is True:
            return pa
    except Exception:  # noqa: BLE001 - 双验证失败保持原链路
        state["second_python_attempted"] = True
        pass
    return None


def _reasoning_off_topic(state: dict) -> bool:
    """跑题检测：推理候选与题面实体词零交集（set1 round1 实证 #28）。

    启发式：答案中长度>=4 的英文词，若 >=60% 不出现在题面（排除通用词），
    且至少 3 个陌生词 → 疑似答非所问（模型串题/幻觉到其他题目）。
    只做**不确定路径**的辅助信号，不覆盖 match（两路一致时相信）。
    """
    import re
    answer = str((state.get("reasoning_result") or {}).get("answer") or "").strip()
    problem = str(state.get("problem") or "")
    if not answer or not problem:
        return False
    _STOPWORDS = {
        "therefore", "hence", "thus", "answer", "result", "final", "then", "when",
        "where", "which", "with", "that", "this", "from", "into", "over", "under",
        "must", "will", "would", "should", "have", "has", "been", "being", "also",
        "each", "every", "some", "such", "there", "these", "those", "first",
    }
    a_words = set(re.findall(r"[A-Za-z]{4,}", answer)) - _STOPWORDS
    p_words = set(re.findall(r"[A-Za-z]{4,}", problem))
    if len(a_words) < 3:
        return False
    unknown = a_words - p_words
    return len(unknown) >= 3 and len(unknown) >= 0.6 * len(a_words)


def _python_support_wants_verify(state: dict, config, python_output: dict,
                                 reasoning_result: dict) -> bool:
    """V4.7 通用 Python 双验证触发（set1 round5 实证 #106/#111：python
    support+闭式对、playoff 未走 → 仲裁选错推理值）。

    与 V4.1 规则可判定版（_rule_judgeable_wants_double_verify）互补：
    V4.1 只管规则可判定题（有 rule 文件）；本函数覆盖**所有题**——
    python evidence=support + python 闭式 + 推理候选存在，且两候选冲突
    （推理非闭式散文 或 两闭式不同）→ 值得花第二次独立代码裁决。
    双代码等价才锁（V3.4 纪律：单代码不可信，round3 回放 9/12 错）。
    预算由 _second_python_check 内部 remaining_hard>300s 闸门控制。
    """
    try:
        from utils.rule_judge import is_closed_form_answer
    except Exception:  # noqa: BLE001
        return False
    if state.get("second_python_done"):
        return False
    pa = str((python_output or {}).get("answer") or "").strip()
    ra = str((reasoning_result or {}).get("answer") or "").strip()
    if not pa or not ra:
        return False
    if (python_output or {}).get("evidence_status") != "support":
        return False
    if bool((python_output or {}).get("authenticity", {}).get("fabricated")):
        return False
    if not is_closed_form_answer(pa):
        return False
    # 推理闭式且与 python 闭式等价 → 已是 match，不走到本分支；这里只处理
    # 冲突：推理非闭式（散文/放弃）或 两闭式数值不同（真冲突需裁决）。
    if is_closed_form_answer(ra):
        try:
            from utils.rule_judge import answers_numerically_equivalent
            eq = answers_numerically_equivalent(pa, ra)
            if eq is True:
                return False  # 已等价（理论上不会走到）
        except Exception:  # noqa: BLE001
            pass
    return True


def _rule_judgeable_wants_double_verify(state: dict, config, python_output: dict,
                                         reasoning_result: dict) -> bool:
    """V4.1 L1 规则可判定题是否需要"第二独立代码"确定性验证。

    round3 回放校准（34 道规则可判定错题）：
    - ❌ 双闭式数值等价不能作为正确性证据：推理与 Python 同源（都是 397B
      生成），会"一致地错"（回放 6/6 全错：#0 '2'≠'3'、#103 '5/9'≠'2/3'）。
    - ❌ 单份 Python 闭式也不能直接锁：回放 12 次触发 9 次选错（#13/#15/
      #18/#34/#35/#42/#72/#76/#104——模型写的代码本身就算错）。
    - ✅ 唯一可靠的确定性证据：**两份独立方法代码**等价（AIMO2 路线）。
      走到本分支的题（uncertain/mismatch/contradict，未 match）只要有
      python 闭式 + 自检 PASS，就强制触发一次"完全不同方法"的第二份代码
      （require_playoff=False），两份代码数学等价才锁 python：
      · #2/#14/#27/#66 类（py 对、rz 错或散文）→ 第二代码与 py 一致 → 救回；
      · #13/#15 类（py 错）→ 第二代码不一致 → 不锁，走原链路（不更差）；
      · 双闭式等价同错的题早已 match 出厂，不走到本分支，不受影响。
    合规：无 rule 文件（官方测试）时 is_rule_judgeable 返回 None → False，
    行为与 V4.0 完全一致。
    """
    try:
        from utils.rule_judge import state_is_rule_judgeable, is_closed_form_answer
        if not state_is_rule_judgeable(state):
            return False
    except Exception:  # noqa: BLE001 - 判定器故障不改变现状
        return False
    if state.get("question_mode") != "computation" \
            and not state.get("question_mode", "").startswith("fill"):
        return False
    pa = str((python_output or {}).get("answer") or "").strip()
    if not pa:
        return False
    if not is_closed_form_answer(pa):
        return False
    if (python_output or {}).get("evidence_status") != "support":
        return False
    if bool((python_output or {}).get("authenticity", {}).get("fabricated")):
        return False
    if bool((python_output or {}).get("skipped")):
        return False
    return True


def _hard_verify_forced(state, config) -> bool:
    """V4.0 L1 强制验证门：hard 题未过任何验证门时，强制至少 1 次 reconciliation
    （突破 reconciliation_retry_available 的轮次限制——"无验证结论不许出厂"
    优先于轮次纪律；预算不足 300s 时放弃强制，防止烧穿墙钟）。"""
    if state.get("difficulty") != "hard":
        return False
    if state.get("reconciliation_round", 0) >= 1:
        return False  # 已重推过一轮
    if state.get("playoff_status") == "decisive":
        return False  # 已有确定性验证
    if state.get("validation_status") == "match":
        return False  # 已有一致结论
    po = state.get("python_output") or {}
    if po.get("evidence_status") == "support":
        return False  # 代码验证通过
    if state.get("second_python_done"):
        return False  # 双验证已用
    try:
        from utils.deps import get_deps
        clock = get_deps(config).time_budget
        if clock is not None and clock.remaining_hard() < 300:
            return False
    except Exception:  # noqa: BLE001 - 预算取不到时按可负担处理
        pass
    return True


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
        from utils.llm_retry import chat_with_retry
        raw = chat_with_retry(
            deps.client,
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
        # V3.6（set1 round2 实证 #4/#46/#58/#91 类）：playoff 已跑且不可决、
        # 重算/仲裁都经常选错时——Python 双验证一致则确定性采信，跳过
        # reconciliation 与 LLM 仲裁（两者对"Python 对但推理错"都无效）。
        if already_played and state.get("playoff_status") != "decisive":
            second = _second_python_check(state, config)
            if second is not None:
                status = "match"
                match_result["status"] = "match"
                match_result["routing_reason"] = "python_double_verification_locked"
                match_result["adopted_source"] = "python"
                validated_answer = second
                next_node = "coordinator"
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
                result["answer_locked"] = False
                result["second_python_done"] = True
                if revote_trace:
                    result["objective_revote_trace"] = revote_trace
                return result
        # V4.1 L1 规则可判定确定性接管（仲裁前的最后确定性防线）：
        # 推理非闭式 + python 闭式 + 自检 PASS → 强制"完全不同方法"的
        # 第二份代码，两份代码数学等价才锁 python（round3 回放校准：
        # 单代码 9/12 错锁、双闭式等价 6/6 同错，都不足信；独立方法
        # 双代码等价是唯一可靠的确定性证据，AIMO2 路线）。
        if _rule_judgeable_wants_double_verify(state, config, python_output, reasoning_result):
            second = _second_python_check(state, config, require_playoff=False)
            if second is not None:
                status = "match"
                match_result["status"] = "match"
                match_result["routing_reason"] = "rule_judgeable_double_python_locked"
                match_result["adopted_source"] = "python"
                match_result["verify_gate"] = "rule_judgeable_takeover"
                validated_answer = second
                next_node = "coordinator"
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
                result["answer_locked"] = True
                result["second_python_done"] = True
                if revote_trace:
                    result["objective_revote_trace"] = revote_trace
                return result
        # V4.0 L1 强制验证门：hard 题未过验证门 → 强制重推（无验证结论不许出厂）
        if _hard_verify_forced(state, config):
            status = "mismatch_reconciling"
            match_result["routing_reason"] = "hard_verify_gate_forced_reconciliation"
            match_result["verify_gate"] = "forced_reconciliation"
            next_node = "reconciliation"
        elif reconciliation_retry_available(state, config):
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
        # V3.2 跑题检测（set1 round1 实证 #28）：推理候选与题面实体词零交集
        # （圆锥杯题答成硬币游戏）时，LLM 仲裁也不可信——若 Python 有可用答案，
        # 确定性锁 Python；否则带"紧扣题面"提示走调解重算。
        if _reasoning_off_topic(state):
            python_answer = str((python_output or {}).get("answer") or "").strip()
            if python_answer and not is_placeholder_answer(python_answer):
                status = "match"
                match_result["status"] = "match"
                match_result["routing_reason"] = "reasoning_off_topic_python_locked"
                match_result["adopted_source"] = "python"
                validated_answer = python_answer
                next_node = "coordinator"
            else:
                status = "mismatch_reconciling"
                match_result["routing_reason"] = "reasoning_off_topic_reconcile"
                match_result["reasoning_retry_hint"] = (
                    "上一轮推理答非所问（答案与题面内容无关）。请严格围绕本题面重新求解，"
                    "不得引用或沿用其他题目的模型/结论。")
                next_node = "reconciliation"
        else:
            # V3.4 Python 双验证（set1 round2 实证：#4/#13/#46/#58/#91 类
            # "Python 对但仲裁未采用"）：playoff 不可决时，两份独立代码结果
            # 等价 → 确定性采信 Python，不再交给 LLM 仲裁（仲裁常误选推理错值）。
            second = _second_python_check(state, config)
            if second is not None:
                status = "match"
                match_result["status"] = "match"
                match_result["routing_reason"] = "python_double_verification_locked"
                match_result["adopted_source"] = "python"
                validated_answer = second
                next_node = "coordinator"
            else:
                # V4.1 L1：仲裁前先试规则可判定确定性接管——推理非闭式 +
                # python 闭式 + support → 强制第二独立代码，双代码等价才锁
                # （round3 回放校准：单代码/双闭式等价均不足信）。
                if _rule_judgeable_wants_double_verify(
                        state, config, python_output, reasoning_result):
                    second = _second_python_check(state, config, require_playoff=False)
                    if second is not None:
                        status = "match"
                        match_result["status"] = "match"
                        match_result["routing_reason"] = "rule_judgeable_double_python_locked"
                        match_result["adopted_source"] = "python"
                        match_result["verify_gate"] = "rule_judgeable_takeover"
                        validated_answer = second
                        next_node = "coordinator"
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
                        result["answer_locked"] = True
                        result["second_python_done"] = True
                        if revote_trace:
                            result["objective_revote_trace"] = revote_trace
                        return result
                # V4.7 通用 Python 双验证（round5 实证 #106/#111：python
                # support+闭式对、playoff 未走 → 仲裁选错推理值）。规则可
                # 判定版只管有 rule 文件的题；这里覆盖所有"python support+
                # 闭式+与推理冲突"的题，同样强制第二独立代码，双代码等价才锁。
                if _python_support_wants_verify(
                        state, config, python_output, reasoning_result):
                    second = _second_python_check(state, config, require_playoff=False)
                    if second is not None:
                        status = "match"
                        match_result["status"] = "match"
                        match_result["routing_reason"] = "python_support_double_locked"
                        match_result["adopted_source"] = "python"
                        match_result["verify_gate"] = "python_support_double"
                        validated_answer = second
                        next_node = "coordinator"
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
                        result["answer_locked"] = True
                        result["second_python_done"] = True
                        if revote_trace:
                            result["objective_revote_trace"] = revote_trace
                        return result
                if _hard_verify_forced(state, config):
                    # V4.0 L1：双验证不可决且未过验证门 → 强制重推
                    status = "mismatch_reconciling"
                    match_result["routing_reason"] = "hard_verify_gate_forced_reconciliation"
                    match_result["verify_gate"] = "forced_reconciliation"
                    next_node = "reconciliation"
                else:
                    # V4.5 全放弃候选跳过仲裁（set1 round4 实证 #24/#44：
                    # 两候选都是"无法确定/缺少条件"，仲裁从两段放弃文案里
                    # 选毫无意义）→ 带重读提示直接重推，省 1 次仲裁调用。
                    ra2 = _clean_answer(reasoning_result.get("answer", ""))
                    pa2 = _clean_answer(python_output.get("answer", ""))
                    _skip_arb = False
                    if CONFIG.get("enable_give_up_skip_arbitration", True) \
                            and reconciliation_retry_available(state, config):
                        try:
                            from utils.hard_fallback import looks_like_give_up
                            from utils.rule_judge import is_closed_form_answer
                            r_give = bool(ra2) and looks_like_give_up(ra2)
                            p_give = bool(pa2) and looks_like_give_up(pa2)
                            both_non_closed = (
                                not is_closed_form_answer(ra2)
                                and not is_closed_form_answer(pa2))
                            if both_non_closed and (r_give or p_give):
                                _skip_arb = True
                        except Exception:  # noqa: BLE001 - 判定失败不跳过仲裁
                            pass
                    if _skip_arb:
                        status = "mismatch_reconciling"
                        match_result["routing_reason"] = "give_up_candidates_skip_arbitration"
                        match_result["reasoning_retry_hint"] = (
                            "上一轮结论为'无法确定/缺少条件'。竞赛题面必然完整，"
                            "请重新逐字阅读题目，找出全部隐含条件，然后完整求解，"
                            "输出明确的最终答案。")
                        # V5.2 假性放弃强制 SC（#24/#13/#14 实证：重读一次
                        # 仍放弃——easy/medium 题模型会做但坚称条件不足，
                        # 强制 SC 多视角采样捞回，简单题采样命中率极高）。
                        match_result["give_up_forced_sc"] = True
                        next_node = "reconciliation"
                    else:
                        status = "uncertain_arbitrating"
                        match_result["routing_reason"] = \
                            "insufficient_equivalence_evidence_semantic_arbitration"
                        next_node = "semantic_arbiter"

    history_entry["status"] = status
    # V4.5 出厂前跑题拦截（set1 round4 实证 #16：串题答了另一道题的
    # 65 位大整数）：match 出厂但答案与题面零实体交集 → 疑似串题，
    # 强制 reconciliation 重推（不直接丢弃；重推仍一致则出厂）。
    # 仅 computation 非客观题 + 未重推过 + 预算充足时触发。
    if status == "match" and validated_answer \
            and not is_objective_mode(question_mode) \
            and state.get("reconciliation_round", 0) < 1 \
            and CONFIG.get("enable_off_topic_gate", True):
        try:
            from utils.off_topic_guard import answer_off_topic
            from utils.deps import get_deps
            _deps = get_deps(config)
            clock = _deps.time_budget
            affordable = clock is None or clock.remaining_hard() > 300
            if affordable and answer_off_topic(
                    state.get("problem", ""), validated_answer):
                status = "mismatch_reconciling"
                match_result["routing_reason"] = "off_topic_gate_forced_reconciliation"
                match_result["verify_gate"] = "off_topic_gate"
                next_node = "reconciliation"
                _deps.logger.warning(
                    "Off-topic gate: answer has no entity overlap with problem -> reconcile")
        except Exception:  # noqa: BLE001 - 跑题拦截失败不改变现状
            pass
    # V4.0 L4 验证证据记录：每题 trace 可见的"过门摘要"（防幻觉可追溯——
    # 出厂答案过了哪些验证门，一目了然；未过门题必须走强制重推/仲裁）。
    match_result["verify_gate"] = {
        "code_support": evidence_status == "support",
        "playoff": state.get("playoff_status"),
        "double_python": bool(state.get("second_python_done")),
        "off_topic": _reasoning_off_topic(state),
        "hard_forced": bool(match_result.get("verify_gate") == "forced_reconciliation"),
    }
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
    if match_result.get("routing_reason") == "python_double_verification_locked":
        # 双验证已用（防 reconciliation 重跑后再次触发第二份代码调用）
        result["second_python_done"] = True
    if revote_trace:
        result["objective_revote_trace"] = revote_trace
    return result
