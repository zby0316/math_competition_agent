"""Python 验证节点：生成 Python 验证代码，经 MCP 执行，自我纠错。

加固要点：当前模型会输出 'Thinking Process:' 思维链，偶尔在散文中间写
'```python'。_extract_code 用严格围栏（要求换行）+ 基于内容的回退，找不到代码
时返回 ''（→ 重试），绝不把思维链当代码执行。
"""
import re
from utils.deps import get_deps
from utils.guard_ceiling import NodeTimeoutError, run_with_timeout
from utils.llm_retry_guard import chat_prefilled, chat_with_retry
from utils.prompt_bank import PYTHON_PROMPT
from utils.score_budget import estimate_tokens
from utils.cot_trim import strip_cot_prefix
from utils.retry_pricing import can_afford_retry, last_attempt_cost
from utils.domain_excerpt import select_script_excerpt, select_skill_excerpt
from utils.verdict_evidence import parse_verification_evidence
from utils.question_profile import is_objective_mode, structure_instruction
from config import CONFIG


def _extract_code(response: str) -> str:
    """从 LLM 响应中提取 Python 代码。

    1. 严格围栏（```python 独占一行、要求换行）——避免把散文中间写的
       '```python code block' 误当成代码。
    2. 宽松围栏，仅当内容看起来像代码时采用。
    3. 基于内容：首个 'import'/'from' 行到结尾（遇闭合 ``` 停止）。
    4. 找不到任何像代码的内容时返回 ''——绝不把思维链当代码执行。
    """
    response = strip_cot_prefix(response)
    m = re.search(r"```python[ \t]*\n(.*?)\n```", response, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```python\s+(.*?)\s+```", response, re.DOTALL)
    if m:
        cand = m.group(1).strip()
        if re.match(r"^\s*(import |from |#|def |class |if |for |while |try |with |@[a-zA-Z]|[a-zA-Z_]\w*\s*=)", cand):
            return cand
    m = re.search(r"```\s+(.*?)\s+```", response, re.DOTALL)
    if m:
        cand = m.group(1).strip()
        if re.match(r"^\s*(import |from |#|def )", cand):
            return cand
    lines = response.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^\s*(import |from )", ln):
            start = i
            break
    if start is not None:
        block = []
        for ln in lines[start:]:
            if ln.strip() == "```":
                break
            block.append(ln)
        return "\n".join(block).strip()
    return ""


_ANSWER_PRINT_RE = re.compile(r"print\s*\(\s*[\"'][^\"']*最终答案")

#: 低于该长度，无代码的响应是格式失误；高于它，说明生成到一半被截断。
#: 与推理分支采用同一阈值。
_EXHAUSTION_MIN_CHARS = 2000


_EVIDENCE_RETRY_CONTRACT = (
    "\n无论本轮是否成功，代码都必须在最终答案前打印两行："
    'print("验证状态:", status)，其中 status 必须是 "PASS"、"FAIL" 或 '
    '"INCONCLUSIVE"；以及 print("验证证据:", evidence)。'
    'FAIL 必须包含反例或失败断言，无法判断时使用 INCONCLUSIVE。'
    '最后一行必须是 print("最终答案:", answer)，不得省略这三类输出。'
)


def _retry_prompt(instruction: str, problem: str, candidate_answer: str = "") -> str:
    """每次短重试都保留证据契约与候选上下文。"""
    candidate_hint = ""
    if candidate_answer:
        candidate_hint = (
            "\n待核验候选（不可信，仅用于独立核对）：\n"
            + candidate_answer[:2000]
            + "\n请不要盲从候选，必须根据独立计算填写验证状态和验证证据。"
        )
    return instruction + _EVIDENCE_RETRY_CONTRACT + candidate_hint + "\n问题：" + problem


def _prints_answer(code: str) -> bool:
    """代码里是否包含规定的答案打印语句。

    一个廉价的截断检测：`max_tokens` 把生成截断在文件中间，会留下看似合法但
    未完成的代码。匹配 print 调用（而非只匹配标记文本）可避免把一条提到
    最终答案 的注释误算作有效。
    """
    return bool(_ANSWER_PRINT_RE.search(code or ""))


_DEF_RE = re.compile(r"(?m)^def\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*(?:->[^:\n]+)?:")
_MAIN_NAME_RE = re.compile(r"(?i)main|solve|count|compute|calc|answer|result|final")


def _append_answer_print(code: str) -> str:
    """定义了函数却没有产出结论的代码，确定性补一个主调用 + 打印。

    曾出现 DFS 建模方向完全正确，但只定义 count_hamiltonian_paths() 未调用、
    stdout 空转 0.058s 的情况；补打印的 LLM 短重试又被预算门禁拒绝。改写是
    确定性的（不花 LLM 时间）：挑一个可无参调用的函数追加
    print("最终答案:", fn())，执行仍受 60s 执行器时限保护。挑不出目标时返回 ""。
    """
    source = code or ""
    if not source.strip():
        return ""
    callable_names = []
    for name, params in _DEF_RE.findall(source):
        params_clean = params.strip()
        if params_clean and not all("=" in part for part in params_clean.split(",")):
            continue  # 需要实参的函数不能盲调
        callable_names.append(name)
    if not callable_names:
        return ""
    preferred = [n for n in callable_names if _MAIN_NAME_RE.search(n)]
    target = (preferred or callable_names)[-1]
    return source + f'\n\nprint("最终答案:", {target}())\n'


#: 首轮 Python 调用的单次墙钟上限。与推理分支同一条规则：难题上首轮会把
#: 整个节点 1100s 上限吃光、被 node_wrapper 掐断后压缩重生成永远没机会触发。
#: 压到 550s，保证首轮 + 压缩重生成(350s 估时) + 余量(250s) 仍能放进 1200s 硬时限。
_FIRST_ATTEMPT_TIMEOUT_S = CONFIG.get("first_attempt_timeout_s", 550)

#: 压缩重生成的估时（16384 token @ ~50 tok/s + 余量）。
_COMPRESSED_CALL_ESTIMATE_S = 350

#: 与推理分支同一条硬时限余量定价规则：软预算已尽也放行压缩重生成，只要求
#: 硬上限前剩余 ≥ 估时 + 收尾余量。
_COMPRESSED_RESERVE_MARGIN_S = CONFIG.get("compressed_safeguard_s", 150)

#: assistant 种子：以代码围栏开头接管助手轮，抑制 reasoning_content（机制同
#: 分类器/仲裁器 prefill），8192 token 全部用于代码本身。
_COMPRESSED_PREFILL = "```python\n"


def _compressed_python_call(deps, problem, candidate_answer,
                            failure="因长度超限在代码之前被截断"):
    """CoT 耗尽/传输超时后的熔断重生成。

    普通重试会以同样方式再截断一次；prefill 压缩调用直接从代码围栏开始续写。
    失败返回 (None, 原因)。
    """
    clock = deps.time_budget
    if clock and clock.remaining_hard() - _COMPRESSED_RESERVE_MARGIN_S \
            < _COMPRESSED_CALL_ESTIMATE_S:
        return None, "compressed_retry_unaffordable"
    instruction = _retry_prompt(
        f"上一次生成{failure}。现在禁止任何解释文字：直接给出一个"
        "**尽可能短**的完整 Python 程序。优先做小规模精确计算（枚举/DP/sympy），"
        "省略探索性打印。",
        problem, candidate_answer)
    try:
        resp = chat_prefilled(
            deps.client,
            messages=[{"role": "user", "content": instruction}],
            prefix=_COMPRESSED_PREFILL,
            temperature=CONFIG["temperatures"]["python"],
            max_tokens=CONFIG["max_tokens"]["python_compressed"],
            logger=deps.logger,
            time_budget=clock,
            expected_call_seconds=_COMPRESSED_CALL_ESTIMATE_S,
            label="python_compressed",
            safeguard_margin_s=_COMPRESSED_RESERVE_MARGIN_S,
        )
    except Exception as exc:  # noqa: BLE001
        deps.logger.warning("Compressed python retry failed: %s", exc)
        return None, f"error: {str(exc)[:160]}"
    if deps.token_budget:
        deps.token_budget.consume(estimate_tokens(instruction), estimate_tokens(resp))
    return resp, ""


def verify_step(state, config):
    deps = get_deps(config)
    sl = deps.skills_loader
    client = deps.client
    mcp_client = deps.mcp_client
    budget = deps.token_budget
    max_attempts = 1 if budget and budget.is_tight() else CONFIG["max_retries_per_node"]
    problem, category = state["problem"], state["category"]
    question_mode = state.get("question_mode", "computation")
    if is_objective_mode(question_mode):
        # 选择/判断/填空由短推理路径作答。为它们生成 Python 程序只会增加延迟，
        # 且常产生无法表达多个选项字母或空位的误导性标量。
        return {
            "python_code": "",
            "python_output": {
                "success": False,
                "skipped": True,
                "stdout": "",
                "stderr": "objective question: python verification skipped",
                "answer": "",
                "execution_time": 0.0,
                "evidence_status": "inconclusive",
                "evidence_summary": "客观题采用快速推理路径，未生成程序。",
                "contradictions": [],
            },
            "python_trace": [{"attempt": 0, "status": "skipped", "reason": "objective_mode"}],
            "python_attempts": 0,
            "python_evidence_status": "inconclusive",
            "python_evidence_summary": "客观题采用快速推理路径，未生成程序。",
            "python_contradictions": [],
        }
    reasoning_result = state.get("reasoning_result") or {}
    candidate_answer = str(reasoning_result.get("answer") or "").strip()
    try:
        validation_script = sl.get_validation_script(category)
    except Exception:
        validation_script = ""
    # 验证示例是提示文档。按知识点章节的词汇与题目匹配来选摘，而不是发送
    # 固定位置的段落前缀。
    base_prompt = PYTHON_PROMPT.format(
        validation_script=select_script_excerpt(validation_script, problem, 2000),
        problem=problem, category=category)
    base_prompt += structure_instruction(problem)
    # 技能手册的速查条目（题型方法论）与验证示例互补：前者决定"算什么/怎么建模"，
    # 后者给"怎么写验证代码"。同一素材源推理分支已在读，这里用小额度共享。
    try:
        skill_doc = sl.get_skill_document(category)
    except Exception:
        skill_doc = ""
    knowledge_ref = select_skill_excerpt(skill_doc, problem, 1600)
    if knowledge_ref:
        base_prompt += "\n\n[技能知识点参考]\n" + knowledge_ref
    # 公开题库检索（开关默认关）：只注入相似题题面（不给解答结论），防止验证
    # 代码照抄参考解。失败/未开启时零影响。
    if CONFIG.get("qbank_enabled"):
        try:
            from utils.qbank import build_reference_block
            py_ref, _ = build_reference_block(
                problem, CONFIG.get("qbank_top_k", 2),
                CONFIG.get("qbank_max_ref_chars", 1400), mode="python")
            if py_ref:
                base_prompt += "\n\n" + py_ref
        except Exception as exc:  # noqa: BLE001 - 检索失败不影响主流程
            deps.logger.warning("QBank python reference unavailable: %s", exc)
    if candidate_answer:
        base_prompt += (
            "\n\n待核验候选（不可信，仅用于独立核对）：\n"
            + candidate_answer[:2000]
            + "\n请不要盲从该候选；根据代码计算结果填写验证状态和验证证据。"
        )
    hint = state.get("branch_hint")
    prompt = f"{base_prompt}\n\n[复核提示] {hint}" if hint else base_prompt

    trace = []
    attempts = 0
    last_output = None
    last_code = ""
    clock = deps.time_budget

    def finalize(output):
        enriched = parse_verification_evidence(
            output, candidate_answer=candidate_answer, code=last_code)
        return {
            "python_code": last_code,
            "python_output": enriched,
            "python_trace": trace,
            "python_attempts": attempts,
            "python_evidence_status": enriched.get("evidence_status", "inconclusive"),
            "python_evidence_summary": enriched.get("evidence_summary", ""),
            "python_contradictions": enriched.get("contradictions", []),
        }
    if clock and clock.fast_path():
        max_attempts = 1
    compressed_next = False
    compressed_used = False
    compressed_failure = "因长度超限在代码之前被截断"
    while True:
        if compressed_next:
            # 熔断重生成不占普通重试名额，也不按上一轮 450s 的实测成本计价——
            # 它的成本上界就是压缩额度本身（~200s），硬时限余量检查在调用内。
            compressed_next = False
            compressed_used = True
            attempts += 1
            resp, comp_fail = _compressed_python_call(deps, problem, candidate_answer,
                                                      failure=compressed_failure)
            if resp is None:
                trace.append({"attempt": attempts, "status": "skipped",
                              "reason": comp_fail or "compressed_retry_failed"})
                break
        else:
            if attempts >= max_attempts:
                break
            # 与推理分支同一条可负担性规则：按上一轮实际成本核拨重试额度。
            # 两侧各自盲目重试 ~510s，正是当年超 deadline 且无答案的原因。
            if attempts and not can_afford_retry(clock, "python"):
                deps.logger.warning(
                    "Skipping Python retry: %.0fs left, last attempt cost %.0fs",
                    clock.remaining(), last_attempt_cost(clock, "python"))
                trace.append({"attempt": attempts + 1, "status": "skipped",
                              "reason": "retry_unaffordable"})
                break
            attempts += 1
            # 与推理分支同一条规则：传输失败时保留本分支已产出的内容，而不是清空
            # 它，这样另一分支不会在无需如此时沦为唯一候选。
            try:
                if attempts == 1 and _FIRST_ATTEMPT_TIMEOUT_S:
                    # 首轮调用加单次墙钟上限：与推理分支同一条规则，难题上首轮
                    # 会把整个节点 1100s 上限吃光、被 node_wrapper 掐断后压缩
                    # 重生成永远没机会触发。压到 550s，超时就地转入压缩重生成。
                    resp = run_with_timeout(
                        lambda: chat_with_retry(
                            client,
                            messages=[{"role": "user", "content": prompt}],
                            temperature=CONFIG["temperatures"]["python"],
                            max_tokens=CONFIG["max_tokens"]["python"],
                            logger=deps.logger,
                            time_budget=deps.time_budget,
                            label="python",
                        ),
                        _FIRST_ATTEMPT_TIMEOUT_S,
                    )
                else:
                    resp = chat_with_retry(
                        client,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=CONFIG["temperatures"]["python"],
                        max_tokens=CONFIG["max_tokens"]["python"],
                        logger=deps.logger,
                        time_budget=deps.time_budget,
                        label="python",
                    )
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, NodeTimeoutError) and clock:
                    # 与推理分支同规则：把首轮超时上限记入账，让对账定价看到完整
                    # 调用的真实成本，而不是压缩重生成的短耗时。
                    clock.record("python", _FIRST_ATTEMPT_TIMEOUT_S)
                deps.logger.warning("Python attempt %s failed: %s", attempts, exc)
                last_output = last_output or {
                    "success": False, "stdout": "", "stderr": str(exc)[:300],
                    "answer": None, "execution_time": 0.0,
                }
                trace.append({"attempt": attempts, "status": "failed", "error": str(exc)[:200]})
                # 首轮超时/传输失败同样先试压缩挽救（与推理分支同规则）：压缩调用
                # 输出短，在并发拥堵下更可能按时返回。
                if not compressed_used:
                    compressed_failure = (
                        "因首轮生成超出时限被切断" if isinstance(exc, NodeTimeoutError)
                        else "因网络传输故障未能返回")
                    compressed_next = True
                    continue
                break
            if budget:
                budget.consume(estimate_tokens(prompt), estimate_tokens(resp))
        code = _extract_code(resp)
        if code:
            last_code = code
        if not code:
            # 响应*很长*却无代码围栏，说明 CoT 在写出任何代码前耗尽了整个额度
            # （当前模型把 reasoning_content 计入 max_tokens）。再问同一程序会以
            # 同样方式截断——这是"无代码/无产出"那一类失败。改发 prefill 压缩
            # 重生成；压缩后仍无代码则放弃本分支。
            # finish_reason=="length" 是服务端明确的截断信号，即使 content 短也
            # 准确；"stop" 是正常结束（无代码只是格式失误，走重问而非压缩）。
            finish_reason = getattr(resp, "finish_reason", None)
            exhausted = (finish_reason == "length"
                         or (finish_reason != "stop"
                             and len(resp or "") >= _EXHAUSTION_MIN_CHARS))
            last_output = {"success": False, "stdout": "",
                           "stderr": "code generation truncated before any code block"
                                     if exhausted else "no code block extracted",
                           "answer": None, "execution_time": 0.0}
            trace.append({"attempt": attempts, "status": "failed",
                          "error": "truncated before code" if exhausted else "no code block",
                          "response_chars": len(resp or "")})
            if compressed_used:
                break
            if exhausted:
                compressed_next = True
                continue
            prompt = _retry_prompt(
                "你上一次没有输出 ```python``` 代码块。请只输出一个 ```python``` 代码块，"
                "不要任何其他文字。用 sympy 计算，最后一行 print(\"最终答案:\", answer)。",
                problem, candidate_answer)
            continue
        if not _prints_answer(code):
            # 缺少最终答案打印的代码**仍然执行**：stdout 里往往已有结论
            # （曾出现正确公式在代码里，旧逻辑拒绝执行去换一次 450s 的重生成，
            # 最终一无所获）。执行后由 stdout 二次抽取兜底。
            trace.append({"attempt": attempts, "status": "missing_answer_print",
                          "note": "executing anyway; stdout miner may recover"})
        if mcp_client is None:
            output = {"success": False, "stdout": "", "stderr": "MCP client unavailable",
                      "answer": None, "execution_time": 0.0}
        else:
            output = mcp_client.execute(code, timeout=CONFIG["node_timeouts"]["python_mcp_execute"])
        last_output = output
        trace.append({
            "attempt": attempts,
            "status": "success" if output.get("success") else "failed",
            "error": (output.get("stderr") or "")[:200],
        })
        if output.get("success"):
            probe = parse_verification_evidence(
                output, candidate_answer=candidate_answer, code=code)
            if str(probe.get("answer") or "").strip():
                return finalize(output)
            # 执行成功但连 stdout 挖掘都拿不到结论 → 先做确定性补调用修复
            # （曾出现代码方向正确、只差一个主调用，而"补打印"LLM 短重试恰恰
            # 会被预算门禁拒绝）。修不动再花 LLM 重试。
            trace.append({"attempt": attempts, "status": "no_extractable_answer"})
            repaired = _append_answer_print(code)
            if repaired and mcp_client is not None:
                repaired_output = mcp_client.execute(
                    repaired, timeout=CONFIG["node_timeouts"]["python_mcp_execute"])
                repaired_probe = parse_verification_evidence(
                    repaired_output, candidate_answer=candidate_answer, code=repaired)
                answer_text = str(repaired_probe.get("answer") or "").strip()
                if repaired_output.get("success") and answer_text \
                        and answer_text.lower() not in {"none", "null"}:
                    last_code = repaired
                    trace.append({"attempt": attempts,
                                  "status": "deterministic_print_repair"})
                    return finalize(repaired_output)
                trace.append({"attempt": attempts, "status": "print_repair_failed",
                              "error": (repaired_output.get("stderr") or "")[:200]})
            if compressed_used:
                return finalize(output)
            prompt = _retry_prompt(
                "你上一次的代码执行成功但没有输出任何最终结论。请输出修正后的 "
                "```python``` 代码块：保留原计算，务必以 print(\"最终答案:\", answer) 结尾。",
                problem, candidate_answer)
            continue
        prompt = _retry_prompt(
            f"代码执行失败：\n错误：{(output.get('stderr') or '')[:500]}\n"
            "请只输出修正后的 ```python``` 代码块，用 sympy，结尾 print(\"最终答案:\", answer)。",
            problem, candidate_answer)
    return finalize(last_output)
