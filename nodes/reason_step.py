import re
from utils.deps import get_deps
from utils.guard_ceiling import NodeTimeoutError, run_with_timeout
from utils.llm_retry_guard import chat_prefilled, chat_with_retry
from utils.prompt_bank import REASONING_PROMPT
from utils.score_budget import estimate_tokens
from utils.answer_pull import (
    extract_answer_fallback_with_source,
    looks_incomplete_answer,
    looks_like_latex_fragment,
    looks_like_process_narration,
)
from utils.verdict_salvage import recover_verdict_sentence
from utils.answer_sanitize import extract_partial_findings
from utils.cot_trim import is_placeholder_answer, strip_cot_prefix
from utils.domain_excerpt import select_skill_excerpt
from utils.retry_pricing import can_afford_retry, last_attempt_cost
from utils.question_profile import (
    answer_coverage_clause,
    is_objective_mode,
    mode_instruction,
    normalize_objective_answer,
    objective_answer_is_usable,
    structure_instruction,
)
from config import CONFIG


def _parse_reasoning_output(response, question_mode="computation"):
    raw = response
    response = strip_cot_prefix(response)
    r = {"analysis": "", "steps": [], "answer": "", "validation_points": []}
    m = re.search(r"## 问题分析\s+(.*?)(?=##|$)", response, re.DOTALL)
    if m:
        r["analysis"] = m.group(1).strip()
    sec = re.search(r"## 详细解题步骤\s+(.*?)(?=## 最终答案|$)", response, re.DOTALL)
    if sec:
        for sm in re.finditer(r"(?:步骤|Step)\s*(\d+)\s*[：:](.*?)(?=(?:步骤|Step)\s*\d+\s*[：:]|##|$)",
                              sec.group(1), re.DOTALL | re.IGNORECASE):
            r["steps"].append({"step_num": int(sm.group(1)), "description": sm.group(2).strip()})
    am = re.search(r"## 最终答案\s+(.*?)(?=##|$)", response, re.DOTALL)
    if am:
        r["answer"] = _distill_answer(am.group(1).strip())
    # 结论速览先行："## 结论速览" 是输出最前的压缩结论（oxed{}），当模型
    # 因 max_tokens 截断、"## 最终答案" 还没写出来时，开头的结论速览仍是
    # 可用的答案落点（gbq 提分核心：答案前置，截断也不丢答案）。仅在"最终
    # 答案"缺失时作为兜底，并打上来源标记，避免与四章节答案混淆。
    if not r["answer"]:
        qc = re.search(r"## 结论速览\s+(.*?)(?=##|$)", response, re.DOTALL)
        if qc:
            r["answer"] = _distill_answer(qc.group(1).strip())
            if r["answer"]:
                r["answer_source"] = "quick_conclusion"
    vm = re.search(r"## 关键验证点\s+(.*?)(?=##|$)", response, re.DOTALL)
    if vm:
        r["validation_points"] = re.findall(r"-\s*(.+)", vm.group(1))
    # 兜底：模型得出了结论却始终没写章节标题。曾在同一道竞赛题上观察到三次，
    # 约 42k 字符的散文里含答案却解析为空，两次运行超过 1000s 也拿不到答案。
    # 仅在标题解析无果时触发，并打上来源标记以便在 trace 中区分捞回答案。
    if not r["answer"]:
        # 优先取显式的 boxed/答案标记，再做散文捞回：短客观回复、以及漏写全部
        # Markdown 标题的长回复都能被正确处理，且不会把中间等式误当成结论。
        # boxed/labelled 是模型显式提交的结论（fallback_marker）；行级扫描
        # （payload_line）置信度与 salvage 同级，标为 fallback_line，耗尽判定
        # 会把它视为"没有产出"（曾实测确认）。
        fallback, tier = extract_answer_fallback_with_source(
            raw if isinstance(raw, str) else response)
        if fallback:
            distilled = _distill_answer(fallback)
            cleaned = _reject_bad_answer(distilled)
            if cleaned:
                r["answer"] = cleaned
                r["answer_source"] = ("fallback_line" if tier == "payload_line"
                                      else "fallback_marker")
    if not r["answer"] and is_objective_mode(question_mode):
        # 普通对话回复偶尔会漏掉 ``答案：`` 标签，只把规范载荷单独放一行
        # （测试替身与裁掉种子前缀的 provider 常见）。在下面逐行恢复该载荷，
        # 但不要把任意推理文字误当成选项列表。
        for line in reversed(str(response or "").splitlines()):
            candidate = line.strip().strip("`*_ ")
            if question_mode == "choice" and not re.fullmatch(
                    r"[A-EＡ-Ｅ](?:\s*[、,，/和及&]\s*[A-EＡ-Ｅ])*[。.]?", candidate,
                    flags=re.I):
                continue
            normalized = normalize_objective_answer(candidate, question_mode)
            if objective_answer_is_usable(normalized, question_mode):
                r["answer"] = normalized
                r["answer_source"] = "bare_objective_line"
                break
    if not r["answer"]:
        salvaged = recover_verdict_sentence(raw if isinstance(raw, str) else response)
        if salvaged and not (CONFIG.get("narration_gate", False)
                             and looks_like_process_narration(salvaged)):
            r["answer"] = salvaged
            r["answer_source"] = "salvaged_prose"
    if is_objective_mode(question_mode) and r["answer"]:
        normalized = normalize_objective_answer(r["answer"], question_mode)
        if objective_answer_is_usable(normalized, question_mode):
            r["answer"] = normalized
        else:
            r["answer"] = ""
    # 精简客观题提示用 ``依据：`` 而非四章节契约。把该证据保留为一条真实步骤，
    # 避免下游质量门与 trace 把一个有效答案当作空响应。
    if r["answer"] and not r["steps"] and is_objective_mode(question_mode):
        evidence = re.search(r"(?:依据|理由|说明)\s*[：:]\s*(.+)", response or "", re.S)
        description = evidence.group(1).strip() if evidence else "按选项/空位逐项核对题面条件。"
        r["steps"] = [{"step_num": 1, "description": description[:600]}]
    return r


def _reject_bad_answer(answer):
    """Placeholder、被截头的 LaTeX 残片、明显不完整或元叙述碎片一律不作为答案。"""
    from utils.answer_sanitize import is_junk

    answer = (answer or "").strip()
    if is_placeholder_answer(answer) or looks_like_latex_fragment(answer) \
            or looks_incomplete_answer(answer) or is_junk(answer):
        return ""
    if CONFIG.get("narration_gate", False) and looks_like_process_narration(answer):
        # 搜救层把"复核过程自述"当答案交付（实测 idx 7：英文复核叙述被当最终
        # 答案）。交付非答案对判分无价值，还会挤掉本可采用的其他候选。
        return ""
    return answer


_ENUM_ITEM_RE = re.compile(r"^\s*(?:\d+\s*[.、)]|[-*•①②③④⑤])")
# 行是否携带答案信息：等式 / 数学式 / 数字 / 结论词。纯引导语（"…如下所示："）丢弃。
_PAYLOAD_LINE_RE = re.compile(r"[=$]|\d|拒绝|接受|显著|结论|因此|所以|故|建议|选择|综上|存在|唯一|收敛|成立")


def _strip_bullet(line):
    return line.lstrip("-*•①②③④⑤ ").strip()


def _join_parts(parts):
    """用全角分号连接各行；行尾自带分号时去重，避免出现 '；；'。"""
    cleaned = [p.rstrip("；; ").strip() for p in parts if p and p.strip("；; ")]
    return "；".join(cleaned)


def _rhs_of_top_level_eq(line):
    """行内最后一个"顶层" '=' 的右侧内容；没有顶层 '=' 时返回 ''。

    顶层 = 不在任何括号/花括号内，且不在 $...$ 数学模式内。这避免了从
    \\sum_{i=1}^n 的下标 'i=1' 或整条 LaTeX 公式中间切断
    （'1}^n X_i$'、'\\sigma^2$ 且无自相关' 均由此产生）。
    """
    depth = 0
    in_math = False
    pos = -1
    for i, ch in enumerate(line):
        if ch == "$":
            in_math = not in_math
        elif ch in "{([":
            depth += 1
        elif ch in "})]":
            depth = max(0, depth - 1)
        elif ch == "=" and depth == 0 and not in_math:
            pos = i
    return line[pos + 1:].strip() if pos >= 0 else ""


def _distill_answer(text):
    """从 '## 最终答案' 节文本中提炼出简洁答案。

    策略顺序（每个候选都要通过 placeholder/残片检查，失败则落到下一策略）：
    1. 枚举节（≥2 个编号/列表项）→ 全部条目整体保留（曾出现只保留 1 个群，
       其余条目被丢的教训）。
    2. 多行短节 → 保留所有信息行（含无 '=' 的结论行——"拒绝/不拒绝 H0" 这类
       结论行曾被只留等式行的旧逻辑丢弃）。
    3. 短节（≤80 字符）→ 最后一行。
    4. 显式 '答案是X' 模式。
    5. 行尾顶层 '=' 右值（LaTeX 感知，绝不切进公式内部）；右值残缺时保留整行。
    6. 首个信息行。
    """
    text = (text or "").strip()
    if is_placeholder_answer(text):
        return ""
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # 1. 枚举节：所有条目整体保留（保留带信息的引导行，如"共 4 种："）
    enum_items = [l for l in lines if _ENUM_ITEM_RE.match(l)]
    if len(enum_items) >= 2 and len(text) <= 1600:
        kept = [_strip_bullet(l) for l in lines
                if _ENUM_ITEM_RE.match(l) or _PAYLOAD_LINE_RE.search(l)]
        joined = _join_parts(kept)
        if not looks_like_latex_fragment(joined) and not is_placeholder_answer(joined):
            return joined

    # 2. 多行短节：保留所有信息行（等式行 + 结论行），过滤纯引导语
    eq_lines = [_strip_bullet(l) for l in lines if "=" in l]
    if 2 <= len(lines) <= 8 and len(text) <= 900 and eq_lines \
            and all(len(l) <= 200 for l in lines):
        kept = [_strip_bullet(l) for l in lines if _PAYLOAD_LINE_RE.search(l)]
        joined = _join_parts(kept)
        if kept and not looks_like_latex_fragment(joined) and not is_placeholder_answer(joined):
            return joined
    if len(eq_lines) >= 3:
        conclusion_extra = [_strip_bullet(l) for l in lines
                            if "=" not in l and re.search(r"拒绝|接受|显著|结论|建议|选择|综上", l)]
        joined = _join_parts(eq_lines + conclusion_extra)
        if len(joined) <= 1600 and not looks_like_latex_fragment(joined) and not is_placeholder_answer(joined):
            return joined

    # 2.5. \boxed 结论：\boxed{...} 是模型显式提交的最终结论，优先于引导语与括注。
    # 曾出现整节「所有…为 / $$\boxed{ab\ge e^3}$$ /（即…）」按旧逻辑落到策略 3
    # 取最后一行括注、把公式丢了。多个 boxed 用「；」连接保留全部结论。
    boxes = re.findall(r"\\boxed\s*\{((?:[^{}]|\{[^{}]*\})*)\}", text)
    if boxes:
        answer = _reject_bad_answer(_join_parts(boxes))
        if answer:
            return answer

    # 3. 短节 → 最后一行
    if len(text) <= 80:
        return _reject_bad_answer(lines[-1] if lines else text)

    # 4. 显式"答案是X"
    for pat in (r"答案[是为：:]\s*(.+?)(?:\n|$)", r"answer\s*[:is]+\s*(.+?)(?:\n|$)"):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            answer = _reject_bad_answer(m.group(1))
            if answer:
                return answer

    # 5. 行尾顶层 '=' 右值；右值残缺时退回整行（不得切进 LaTeX 公式内部）
    eq_line = next((l for l in reversed(lines) if "=" in l), "")
    if eq_line:
        answer = _reject_bad_answer(_rhs_of_top_level_eq(eq_line))
        if answer:
            return answer
        answer = _reject_bad_answer(_strip_bullet(eq_line))
        if answer:
            return answer

    # 6. 首个信息行，最后兜底整节
    for cand in ([l for l in lines if _PAYLOAD_LINE_RE.search(l)][:1]
                 + ([lines[0]] if lines else [])
                 + ([text] if len(text) <= 600 else [])):
        answer = _reject_bad_answer(cand)
        if answer:
            return answer
    return ""


def _is_complete(r):
    return bool(r["answer"]) and len(r["steps"]) >= 1


#: 低于该长度，无章节的响应是短拒绝或格式失误，而非写到一半被截断；
#: 高于它，说明模型还在写就被掐断了。
_EXHAUSTION_MIN_CHARS = 2000


#: 捞回来源的答案不构成"真实产出"：它们是从截断散文里扫出的残句，置信度
#: 不足以取消压缩重试（曾实测：捞回先于耗尽判定，使压缩重试 0 次触发，
#: 数题以中间句出厂）。
_SCAVENGED_SOURCES = ("salvaged_prose", "fallback_line")


def _looks_token_exhausted(response: str, parsed: dict) -> bool:
    """判断响应是死于 `max_tokens` 上限，还是纯粹没看懂格式。

    当前模型把 reasoning_content 计入 max_tokens，难题上可能把整个 32768
    额度全花在私有推理上，一个 Markdown 章节都不输出。特征是响应很长却一无所获：
    没有分析、没有步骤、没有答案；真正的格式错误仍会产生某个可解析章节，
    而拒绝响应很短。

    捞回答案（salvaged_prose/fallback_line）不算产出：判定必须以"捞回之前"
    的解析状态为准，否则一条被扫描到的中间句就会掩盖真实的耗尽、跳过唯一
    有效的挽救手段（压缩重试），让残片直接出厂。
    """
    answer = parsed.get("answer")
    if answer and parsed.get("answer_source") not in _SCAVENGED_SOURCES:
        return False
    if parsed.get("analysis") or parsed.get("steps"):
        return False

    # 优先读服务端真实停止原因（ChatResult.finish_reason）："length" 是明确的
    # max_tokens 截断——即使 content 很短（CoT 吃光额度、可见章节几乎没写），
    # 也能准确捕获，而长度启发式会漏判；"stop" 是正常结束——响应再长也只是
    # 格式失误，该走格式提醒重试而非压缩重试。
    finish_reason = getattr(response, "finish_reason", None)
    if finish_reason == "length":
        return "## 最终答案" not in (response or "")
    if finish_reason == "stop":
        return False

    # 无 finish_reason（平台 client / 传输失败）：退回长度启发式。
    text = response or ""
    if len(text) < _EXHAUSTION_MIN_CHARS:
        return False
    return "## 最终答案" not in text


#: 压缩重试的估时：16384 token 上限 @ ~50 tok/s，再留传输余量。
_COMPRESSED_CALL_ESTIMATE_S = 350

#: 压缩重试允许动用 safeguard，但必须给收尾链留出这些硬时限余量（仲裁 prefill
#: 配额 75s + 应急直答 + 确定性拼装）。曾实测：首轮推理 700-980s 后软预算已尽，
#: 按软预算定价的压缩重试 0 次放行，数题以捞回残片出厂——"保输出"保的必须是
#: 正确的输出。safeguard(300s) 里划 ~200s 给一次压缩重试后，仍有 node_wrapper
#: 的硬超时兜底（节点在 remaining_hard 处被掐断走 fallback），最坏情况不会
#: 击穿平台 20 分钟上限。
_COMPRESSED_RESERVE_MARGIN_S = CONFIG.get("compressed_safeguard_s", 150)

#: 首轮推理调用的单次墙钟上限。难题上首轮会把整个节点 1100s 上限吃光、被
#: node_wrapper 掐断，压缩重试（真正的挽救手段）永远没机会触发（曾实测
#: idx 0/7/11/12/13 均报 "operation timed out after 1100s"、attempts=0、无压缩
#: 重试记录，最终落 emergency_direct_answer 错答）。把它压到 550s，保证首轮 +
#: 压缩重试(350s 估时) + 余量(250s) 仍能放进 1200s 硬时限，让压缩重试真正可达。
_FIRST_ATTEMPT_TIMEOUT_S = CONFIG.get("first_attempt_timeout_s", 550)

#: 压缩重试的 assistant 种子。以内容开头接管助手轮，模型进入续写模式后不再打开
#: reasoning_content（与分类器/仲裁器 prefill 同机制，见 utils/prefill.py 实测），
#: 因此 8192 token 全部落在四章节上。种子从"## 结论速览"开始：先让模型
#: 把结论写出来（答案前置），再展开后续章节，截断也不丢答案（gbq 提分核心）。
_COMPRESSED_PREFILL = "## 结论速览\n\\boxed{"

#: 模型未真正解题、以"题面不完整"类语言敷衍的跑偏信号（对齐 awa 同类防护：
#: 修正轮输出"假设题目/缺少具体题目"→ 丢弃该轮保留原解）。这类响应再做格式
#: 重试只会再烧一次同等耗时且同样跑偏；prefill 压缩重试的强约束（种子 +
#: "必须给出最可信具体结论"指令）才能把它拉回。
_DRIFT_KEYWORDS = ("假设题目", "缺少具体的", "没有提供", "如果您有具体",
                   "题目不完整", "请提供", "缺少条件", "题面缺失")


def _looks_drifted(text) -> bool:
    return any(kw in str(text or "")[:2000] for kw in _DRIFT_KEYWORDS)

_COMPRESSED_INSTRUCTION = (
    "\n\n注意：上一次输出{failure}。现在禁止展开探索性思考："
    "沿最可行的一条路线直接写出四个章节，每章从简（'## 详细解题步骤' 最多 4 步、每步 3 行以内）。"
    "'## 最终答案' 必须给出明确结论；若尚不能完全确定，也必须给出当前最可信的具体结论"
    "（数值/表达式/集合），并把不确定之处写进 '## 关键验证点'，不得留空。{coverage}"
)


def _extract_clues(text, limit=2000):
    """从首轮响应提取模型已算出的有效结论，作为续写/压缩重试的线索。

    散文泄漏（模型把私有 CoT 写进 content）里往往已经一路推导到了关键结论，
    只是没来得及写进 '## 最终答案' 章节就被截断。这些结论可信、可直接复用，
    让续写/重试不必从头重算。
    """
    parts = []
    salvaged = recover_verdict_sentence(text)
    if salvaged:
        parts.append(salvaged)
    findings = extract_partial_findings(text, limit_chars=1200)
    if findings:
        parts.append(findings)
    if not parts:
        return ""
    return (
        "\n\n你上一次推导已经得到以下结论（可信，直接复用，不要再重新推导）：\n"
        + "\n".join(parts)[:limit]
    )


def _compressed_reasoning_retry(deps, base_prompt,
                                failure="因长度超限被截断，没有产出任何章节",
                                coverage="", first_resp=None):
    """Token 耗尽/传输超时后的阶段性熔断。

    普通重试会以同样的方式再耗尽一次 ~450s；prefill 压缩重试把整份额度花在
    可见输出上，~150s 内产出可解析的四章节。定价走硬时限余量模式：软预算已尽
    也放行，只要硬上限前还容得下本次调用 + 收尾余量。

    ``coverage`` 是必须保全的答案覆盖度（全部解支/多问项/计数）——压缩针对推导
    篇幅，绝不针对答案本身该覆盖的内容（曾出现漏值事故）。
    失败时返回 (None, 原因)。
    """
    clock = deps.time_budget
    if clock and clock.remaining_hard() - _COMPRESSED_RESERVE_MARGIN_S \
            < _COMPRESSED_CALL_ESTIMATE_S:
        return None, "compressed_retry_unaffordable"
    instruction = _COMPRESSED_INSTRUCTION.format(failure=failure, coverage=coverage)
    # 复用首轮已算出的结论：压缩重试不再从头重解，而是把首轮 CoT 里已经
    # 推导出的关键结果作为线索注入（曾实测 idx 0 首轮已算出 T=29，但从头
    # 重解却产出 44+√(6109/90) 的错误值）。
    clue_block = _extract_clues(first_resp) if first_resp else ""
    try:
        resp = chat_prefilled(
            deps.client,
            messages=[{"role": "user", "content": base_prompt + clue_block + instruction}],
            prefix=_COMPRESSED_PREFILL,
            temperature=CONFIG["temperatures"]["reasoning"],
            max_tokens=CONFIG["max_tokens"]["reasoning_compressed"],
            logger=deps.logger,
            time_budget=clock,
            expected_call_seconds=_COMPRESSED_CALL_ESTIMATE_S,
            label="reasoning_compressed",
            safeguard_margin_s=_COMPRESSED_RESERVE_MARGIN_S,
        )
    except Exception as exc:  # noqa: BLE001 - degrade to whatever the caller holds.
        deps.logger.warning("Compressed reasoning retry failed: %s", exc)
        return None, f"error: {str(exc)[:160]}"
    if deps.token_budget:
        deps.token_budget.consume(estimate_tokens(base_prompt), estimate_tokens(resp))
    return resp, ""


def _raw_excerpt(text, head=600, tail=1400):
    """截断响应的可审计节选：头部保留题解开场，尾部保留结论区。"""
    s = str(text or "")
    if len(s) <= head + tail + 40:
        return s
    omitted = len(s) - head - tail
    return f"{s[:head]}\n……[中略 {omitted} 字符]……\n{s[-tail:]}"


def reason_step(state, config):
    deps = get_deps(config)
    sl = deps.skills_loader
    client = deps.client
    budget = deps.token_budget
    max_attempts = 1 if budget and budget.is_tight() else CONFIG["max_retries_per_node"]
    problem, category = state["problem"], state["category"]
    question_mode = state.get("question_mode", "computation")
    skill_doc = sl.get_skill_document(category)
    # 按主题选摘，而不是按位置截断。裸 [:3000] 切片只取到文档开头模块，当
    # 非基础及进阶课程长到 8949 字符后，其数论（offset 3404）与博弈论（offset 5369）
    # 模块再也到不了模型——博弈题会拿到 Lebesgue 测度。
    base_prompt = REASONING_PROMPT.format(
        category=category,
        skill_document=select_skill_excerpt(skill_doc, problem, 3000),
        problem=problem)
    base_prompt += mode_instruction(question_mode)
    base_prompt += structure_instruction(problem)
    # 压缩重试要压的是推导篇幅，不是答案该覆盖的分支（曾出现漏值事故）。
    coverage_clause = answer_coverage_clause(problem)

    if is_objective_mode(question_mode):
        # 客观题不需要 30k token 的证明搜索，也不需要独立的 Python 程序。主路径
        # 用一次小规模普通调用：模型仍需足够的可见推理来区分相近的教材选项
        # （例如方差 vs 标准差、非线性回归 vs 非参数回归）。普通响应为空或格式
        # 异常时，保留一次种子调用作为格式安全的兜底。
        domain_guard = ""
        if category == "抽象代数" and re.search(
            r"分裂域|Galois|伽罗瓦|域扩张|扩张次数|单位根", problem, flags=re.I
        ):
            # 曾出现本守卫已存在、模型仍答 Q(∜5, i) 与次数 8 的情况。
            # 补上可机械执行的判据（ζ_n 的实部/虚部展开、√2 的归属、塔次数乘积
            # 与根个数的对账），把"要检查"变成"照着算"。
            domain_guard = (
                "若涉及域扩张或分裂域，按以下顺序机械执行，不得跳步："
                "(1) 写出多项式的**全部**根（n 个），形如 r·ζ_n^j；"
                "(2) 分裂域 = Q(实根, ζ_n)，其中 ζ_n 必须写成 ζ_n 本身而**不是 i**——"
                "ζ_8=(√2/2)(1+i) 含 √2，而 Q(i) 不含 √2，故 x⁴−a 型的分裂域是 "
                "Q(⁴√a, ζ_8) 不是 Q(⁴√a, i)；(3) 用塔定理把次数写成各层次数之积，"
                "并核对 [E:Q] 必须能被单个根的次数整除、且不小于根的个数；"
                "(4) 特征零上的可分多项式，其分裂域必为 Galois 扩张。"
            )
        # 空位数由代码侧数好写进提示（曾出现提示里的"先数空位"元指令被模型原样
        # 回显成了答案 "Count blanks: 1"；以及三空只答零空未被拦截）。
        from utils.question_profile import count_blanks, fill_answer_matches_blanks

        blank_count = count_blanks(problem) if question_mode == "fill" else 0
        if question_mode == "fill":
            blank_note = (
                f"题面共 {blank_count} 个空位。" if blank_count >= 2 else ""
            )
            answer_shape = (
                f"{blank_note}答案行按空位顺序写全部结果，多空用分号分隔"
                "（如：空1: <结果>；空2: <结果>）。每空必须填教材术语/数值/明确方向，"
                "禁止填'不确定'，禁止输出任何操作说明文字"
            )
        elif question_mode == "true_false":
            answer_shape = "答案行只写：正确 或 错误"
        else:
            answer_shape = (
                "先判断单选/多选（题面注明多选或多个选项独立成立才多选），"
                "答案行列出全部正确选项字母；措辞不精确或概念被替换的选项一律不选"
            )
        # 两行契约把"答案"排在"依据"前面，模型先承诺后核对——对需要实算的客观题
        # （域扩张次数、维数、阶、收敛半径等）这个顺序恰好是反的：曾出现先写下
        # Q(∜5,i);8 再补依据。检出实算标记时改为"先核对后作答"，输出契约不变
        # （解析取最后一个"答案："标签），成本只多几行核对文字。
        needs_derivation = bool(re.search(
            r"分裂域|扩张次数|\[E\s*[:：]\s*(?:Q|\\mathbb\{Q\})\]|Galois|伽罗瓦|"
            r"维数|秩|阶数|特征值|收敛半径|次数是|degree\b|dimension\b",
            problem, flags=re.I))
        if needs_derivation:
            contract = (
                "先算后答：必须严格按此顺序输出三行，不要输出 Thinking Process：\n"
                "核对：<逐层写出关键计算（列全部根/逐层次数/定义核对），不超过五行>\n"
                f"答案：<{answer_shape}>\n"
                "依据：<一句话说明答案与上面核对的一致性>"
            )
        else:
            contract = (
                "不要展开长篇推导，不要输出 Thinking Process。"
                "必须只输出两行：\n"
                f"答案：<{answer_shape}>\n"
                "依据：<不超过三句的定理/计算核对>"
            )
        objective_prompt = (
            "你是数学、统计学与计量经济学教师。严格按题库教材定义逐项核对题面选项/空位，"
            "特别检查相近概念的边界（非参数回归不等于非线性回归，标准差与方差按技能口径区分）；"
            f"{domain_guard}"
            f"{contract}\n\n"
            f"题型：{question_mode}\n学科：{category}\n技能参考：\n"
            f"{select_skill_excerpt(skill_doc, problem, 2200)}\n\n题目：\n{problem}"
        )
        trace = []
        response = ""
        attempts = 0
        try:
            attempts = 1
            response = chat_with_retry(
                client,
                messages=[{"role": "user", "content": objective_prompt}],
                temperature=CONFIG["temperatures"].get(
                    "objective_reasoning", CONFIG["temperatures"]["reasoning"]),
                max_tokens=CONFIG["max_tokens"]["objective_reasoning"],
                logger=deps.logger,
                time_budget=deps.time_budget,
                expected_call_seconds=45,
                label="objective_reasoning_plain",
            )
            if budget:
                budget.consume(estimate_tokens(objective_prompt), estimate_tokens(response))
        except Exception as exc:  # noqa: BLE001 - degrade to the normal parser below.
            deps.logger.warning("Objective reasoning failed: %s", exc)
            trace.append({"attempt": attempts, "status": "failed", "error": str(exc)[:200]})
        parsed = _parse_reasoning_output(response, question_mode=question_mode)
        fill_complete = question_mode != "fill" or fill_answer_matches_blanks(
            parsed.get("answer", ""), problem)
        if parsed.get("answer") and fill_complete:
            trace.append({"attempt": attempts, "status": "objective_plain",
                          "response_chars": len(response or "")})
            return {"reasoning_result": parsed, "reasoning_trace": trace,
                    "reasoning_attempts": attempts, "reasoning_raw_response": response}
        if not parsed.get("answer"):
            # 解析失败的原始响应必须留证（75-212 字符的失败响应如果只记字符数，
            # 事后无法区分"模型答错"与"解析器漏掉"）。
            trace.append({"attempt": attempts, "status": "unparsed",
                          "response_chars": len(response or ""),
                          "raw_excerpt": _raw_excerpt(response, head=300, tail=500)})
        if parsed.get("answer") and not fill_complete:
            # 空位覆盖不足（曾出现三空只答零空仍以 0.78 置信放行）。
            # 保留这个不完整候选，但先花一次种子重试争取全空覆盖。
            trace.append({"attempt": attempts, "status": "fill_blank_mismatch",
                          "blanks": count_blanks(problem),
                          "response_chars": len(response or "")})
        incomplete_first = parsed if parsed.get("answer") else None
        # 普通响应违反两行契约时，改发一次种子助手轮。它仍然有界，且比整段证明
        # 生成更省；通用解析器会保留它产出的任何答案。
        if not (deps.time_budget and deps.time_budget.fast_path()):
            try:
                attempts += 1
                response = chat_prefilled(
                    client,
                    messages=[{"role": "user", "content": objective_prompt}],
                    prefix="答案：",
                    temperature=CONFIG["temperatures"].get(
                        "objective_reasoning", CONFIG["temperatures"]["reasoning"]),
                    max_tokens=CONFIG["max_tokens"]["objective_reasoning"],
                    logger=deps.logger,
                    time_budget=deps.time_budget,
                    expected_call_seconds=60,
                    label="objective_reasoning_prefill",
                )
                if budget:
                    budget.consume(estimate_tokens(objective_prompt), estimate_tokens(response))
            except Exception as exc:  # noqa: BLE001
                deps.logger.warning("Objective reasoning retry failed: %s", exc)
                trace.append({"attempt": attempts, "status": "failed", "error": str(exc)[:200]})
            retry_parsed = _parse_reasoning_output(response, question_mode=question_mode)
            retry_fill_complete = question_mode != "fill" or fill_answer_matches_blanks(
                retry_parsed.get("answer", ""), problem)
            # 重试答案只有在"有答案且不比首轮更残缺"时才替换首轮候选。
            if retry_parsed.get("answer") and (retry_fill_complete or incomplete_first is None):
                parsed = retry_parsed
            elif incomplete_first is not None:
                parsed = incomplete_first
        if not parsed.get("answer") and not (deps.time_budget and deps.time_budget.fast_path()):
            # 两次短响应都解析失败（曾出现多选 3 连败，response 仅 75-212 字符）：
            # 升级为一次四章节压缩推理。prefill 抑制私有 CoT，"## 最终答案" 章节
            # 给归一化器一个真正的答案行；客观题路径此时耗时尚少，压缩额度负担得起。
            attempts += 1
            esc_resp, esc_fail = _compressed_reasoning_retry(
                deps, base_prompt, failure="过短且未能解析出答案行")
            if esc_resp is None:
                trace.append({"attempt": attempts, "status": "skipped",
                              "reason": esc_fail or "objective_escalation_failed"})
            else:
                esc_parsed = _parse_reasoning_output(esc_resp, question_mode=question_mode)
                esc_fill_complete = question_mode != "fill" or fill_answer_matches_blanks(
                    esc_parsed.get("answer", ""), problem)
                entry = {"attempt": attempts,
                         "status": "success" if esc_parsed.get("answer") else "failed",
                         "reason": "objective_full_cot_escalation",
                         "response_chars": len(esc_resp or "")}
                if esc_parsed.get("answer") and (esc_fill_complete or incomplete_first is None):
                    parsed = esc_parsed
                    response = esc_resp
                elif not esc_parsed.get("answer"):
                    entry["raw_excerpt"] = _raw_excerpt(esc_resp, head=300, tail=500)
                trace.append(entry)
        final_entry = {"attempt": attempts,
                       "status": "success" if parsed.get("answer") else "failed",
                       "objective": True, "response_chars": len(response or "")}
        if not parsed.get("answer"):
            final_entry["raw_excerpt"] = _raw_excerpt(response, head=300, tail=500)
        trace.append(final_entry)
        return {"reasoning_result": parsed, "reasoning_trace": trace,
                "reasoning_attempts": attempts, "reasoning_raw_response": response}

    hint = state.get("branch_hint")
    # 复核重试也保留 skill 文档与字段契约（旧实现只发 hint+题面，丢失了学科口径）
    prompt = f"{base_prompt}\n\n[复核提示] {hint}" if hint else base_prompt
    hinted_base = prompt
    trace = []
    attempts = 0
    resp = ""
    parsed = {"analysis": "", "steps": [], "answer": "", "validation_points": []}
    # 推理调用是图中最贵的一步（实测中等难度 77-116s，竞赛级 510-552s）。
    # 时间紧张时，格式修复式重试不可承受。
    clock = deps.time_budget
    if clock and clock.fast_path():
        max_attempts = 1
    for _ in range(max_attempts):
        # 重试额度按上一轮实际成本核拨，而不是按"时间还没用完"核拨。否则会出现在
        # 只剩 349s 时又批准一次 545s 的尝试，超 deadline 198s 且最终无答案
        # （竞赛级题）。
        if attempts and not can_afford_retry(clock, "reasoning"):
            deps.logger.warning(
                "Skipping reasoning retry: %.0fs left, last attempt cost %.0fs",
                clock.remaining(), last_attempt_cost(clock, "reasoning"))
            trace.append({"attempt": attempts + 1, "status": "skipped",
                          "reason": "retry_unaffordable"})
            break
        attempts += 1
        # 传输失败不得丢弃上一轮已解析的部分结果。曾因此丢过整个推理分支：异常
        # 从节点冒出后 wrapper 用空结果顶替，交叉验证只看到占位符，只剩一个候选。
        try:
            if attempts == 1 and _FIRST_ATTEMPT_TIMEOUT_S:
                # 首轮调用加单次墙钟上限：难题上首轮会把整个节点 1100s 上限吃光、
                # 被 node_wrapper 掐断后压缩重试永远没机会触发。压到 550s 后，
                # 超时就地转入压缩重试，而不是让 node_wrapper 掐死整条分支。
                resp = run_with_timeout(
                    lambda: chat_with_retry(
                        client,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=CONFIG["temperatures"]["reasoning"],
                        max_tokens=CONFIG["max_tokens"]["reasoning"],
                        logger=deps.logger,
                        time_budget=deps.time_budget,
                        label="reasoning",
                    ),
                    _FIRST_ATTEMPT_TIMEOUT_S,
                )
            else:
                resp = chat_with_retry(
                    client,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=CONFIG["temperatures"]["reasoning"],
                    max_tokens=CONFIG["max_tokens"]["reasoning"],
                    logger=deps.logger,
                    time_budget=deps.time_budget,
                    label="reasoning",
                )
        except Exception as exc:  # noqa: BLE001 - keep whatever we already parsed.
            is_first_timeout = isinstance(exc, NodeTimeoutError)
            if is_first_timeout and clock:
                # 首轮被单次墙钟上限切断：把上限耗时记入账，让对账/重试定价看到
                # 完整调用的真实成本（而非把后续压缩重试的短耗时误当成便宜）。
                clock.record("reasoning", _FIRST_ATTEMPT_TIMEOUT_S)
            deps.logger.warning("Reasoning attempt %s failed: %s", attempts, exc)
            trace.append({"attempt": attempts, "status": "failed", "error": str(exc)[:200]})
            # 首轮超时/传输失败（8 路并发下 780s 读超时为主）不再直接弃分支：
            # 压缩 prefill 重试输出短、在拥堵下更可能按时返回，且按硬时限余量
            # 定价——放弃只该发生在时间真不够时。
            attempts += 1
            rescued, fail_reason = _compressed_reasoning_retry(
                deps, hinted_base,
                failure="因首轮生成超出时限被切断" if is_first_timeout else "因网络传输故障未能返回",
                coverage=coverage_clause)
            if rescued is None:
                trace.append({"attempt": attempts, "status": "skipped",
                              "reason": fail_reason or "compressed_rescue_failed"})
                break
            resp = rescued
            rescued_parsed = _parse_reasoning_output(resp, question_mode=question_mode)
            if rescued_parsed.get("answer") or rescued_parsed.get("steps") \
                    or rescued_parsed.get("analysis"):
                parsed = rescued_parsed
                parsed["answer_source"] = parsed.get("answer_source") or "compressed_prefill"
            entry = {"attempt": attempts,
                     "status": "success" if _is_complete(rescued_parsed) else "failed",
                     "reason": ("compressed_prefill_after_first_attempt_timeout"
                                if is_first_timeout else "compressed_prefill_after_transport_failure"),
                     "response_chars": len(resp or "")}
            if not _is_complete(rescued_parsed):
                entry["raw_excerpt"] = _raw_excerpt(resp)
            trace.append(entry)
            break
        if budget:
            budget.consume(estimate_tokens(prompt), estimate_tokens(resp))
        parsed = _parse_reasoning_output(resp, question_mode=question_mode)
        exhausted = _looks_token_exhausted(resp, parsed)
        trace.append({"attempt": attempts,
                      "status": "success" if _is_complete(parsed) else "failed",
                      "response_chars": len(resp or ""),
                      **({"reason": "token_budget_exhausted",
                          "raw_excerpt": _raw_excerpt(resp)} if exhausted else {})})
        if _is_complete(parsed):
            return {"reasoning_result": parsed, "reasoning_trace": trace,
                    "reasoning_attempts": attempts, "reasoning_raw_response": resp}
        # 两种失败需要两种重试。格式失误值得带格式提醒重问一次；Token 耗尽则不然：
        # 难题上模型可能把整个额度花在 reasoning_content 上、一个章节都不输出
        # （曾实测：550s、~30k tokens、analysis_len=0、steps=0），同样的提示会以
        # 同样方式截断——普通重试只是再烧 550s 而一无所获。这类"耗尽后普通重试"
        # 还常因预算不足被跳过，大量题四章节全空。现改为 prefill 压缩重试：助手
        # 种子抑制私有推理，~150s 即产出可解析章节（阶段性熔断）。
        # [防跑偏] 模型以"题面不完整"敷衍（未解题）同样不做格式重试：跑偏与耗尽
        # 一样会以同样方式复发，压缩重试的强约束才能把它拉回。
        drifted = not parsed.get("answer") and _looks_drifted(resp)
        if drifted:
            trace.append({"attempt": attempts, "status": "drifted",
                          "response_chars": len(resp or ""),
                          "raw_excerpt": _raw_excerpt(resp)})
        if exhausted or drifted:
            attempts += 1
            compressed_resp, fail_reason = _compressed_reasoning_retry(
                deps, hinted_base, coverage=coverage_clause, first_resp=resp)
            if compressed_resp is None:
                trace.append({"attempt": attempts, "status": "skipped",
                              "reason": fail_reason or "compressed_retry_failed"})
                break
            resp = compressed_resp
            compressed_parsed = _parse_reasoning_output(resp, question_mode=question_mode)
            # 压缩重试产出的任何章节都严格优于上一轮的空产出。
            if compressed_parsed.get("answer") or compressed_parsed.get("steps") \
                    or compressed_parsed.get("analysis"):
                if not compressed_parsed.get("answer") and parsed.get("answer"):
                    # 压缩重试补齐章节但没写出结论时，保留此前捞回的低置信答案
                    # （来源标记不变），聊胜于无。
                    compressed_parsed["answer"] = parsed["answer"]
                    compressed_parsed["answer_source"] = parsed.get(
                        "answer_source", "salvaged_prose")
                parsed = compressed_parsed
                parsed["answer_source"] = parsed.get("answer_source") or "compressed_prefill"
            trace.append({"attempt": attempts,
                          "status": "success" if _is_complete(compressed_parsed) else "failed",
                          "reason": ("compressed_prefill_after_exhaustion" if exhausted
                                     else "compressed_prefill_after_drift"),
                          "response_chars": len(resp or "")})
            if _is_complete(compressed_parsed):
                return {"reasoning_result": parsed, "reasoning_trace": trace,
                        "reasoning_attempts": attempts, "reasoning_raw_response": resp}
            break
        prompt = (base_prompt + "\n\n注意：上一次输出缺少必需章节（必须含 '## 问题分析'、'## 详细解题步骤'、"
                  "'## 最终答案'）。'## 最终答案' 下必须按题面要求完整列出各字段/各问项的结果"
                  "（含检验结论、区间上下限、全部枚举对象等），不得只给单个数值。请重新严格按格式输出。")
    return {"reasoning_result": parsed, "reasoning_trace": trace,
            "reasoning_attempts": attempts, "reasoning_raw_response": resp}
