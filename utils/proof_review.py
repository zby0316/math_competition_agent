"""Proof handoff and model-review metadata, never a proof correctness oracle."""
import json
import re
from utils.cot_trim import contains_cot_marker, strip_cot_prefix

CHECKLIST = """证明核查：逐问确认定理前提、量词顺序、阈值方向、端点、序列首项及标量域。
逐项落实自然语言小问与条件小问；参数范围和适用条件也是答卷内容。
一般测度空间的 a.e. 例外须由可测零测包络覆盖；未声明完备时，实际坏点集可能不可测，不能直接对其取测度。
区分已知收敛与待证收敛；已有可测事件足够时直接使用，不添加未经论证的代表存在分支。
确需极限代表时，须给出构造和可测性论证；在整个可测零测包络上固定赋值，恢复原函数的结论时将包络并入例外集，不能只修改实际坏点集，也不能用待证结论证明代表存在。
变换后收敛须重新论证，不能把原序列的收敛无条件传给不连续硬截断。
复数内积不能直接排序或取 liminf；按固定内积约定使用实部、绝对值与共轭。
第一变量线性时，标准序列内积为 sum(u_k * conjugate(v_k))；第二变量线性时为 sum(conjugate(u_k) * v_k)。
坐标式从所声明的定义推出，再用纯虚数代入检查，不凭记忆切换约定。
反例须给出全部对象、适用编号及每项假设的验证；映射须标明定义域、值域及原像方向，拓扑说明须由开集原像核对。
单射、满射和开闭映射等附加属性分别从定义或已证明事实推出，不能由另一个性质或集合等式擅自增加。
集合包含仅能推出在并集上的积分界；检查拆分后的交叉项、常数和共同阈值。
逐项区分“每个编号”“充分大编号”“无穷多个编号”；相邻闭区间可能共享端点。
开球成员须满足距离严格小于半径；非严格界不能直接推出成员关系。须缩小半径或另证边界包含。
严格与非严格不等式逐项核对；在等号处代入定义验证分支，先确认端点再合并区间。
正文中的编号、参数范围和条件贯穿摘要与结论，充分大编号不能升级为全编号恒等式；不要添加未经证明且题面不要求的旁支断言。
接受其他有效证明路线，不得为满足格式而虚构证明。"""


def _scalar_scope(text):
    text = str(text or "")
    if re.search(r"复数|实或复|复\s*(?:Hilbert|Banach|内积|线性|空间)|complex|ℂ|\\mathbb\s*\{?C\}?", text, re.I):
        return "complex"
    if re.search(r"实\s*(?:Hilbert|Banach|内积|线性|空间)|实数|real\s+(?:Hilbert|Banach|inner[ -]product|vector|space)|ℝ|\\mathbb\s*\{?R\}?", text, re.I):
        return "real"
    return "unspecified"


def _method_tasks(text, category=""):
    """Bounded advisory methods; no claim recognition or mathematical verdict."""
    text = str(text or "")
    category = str(category or "")
    def matches(pattern):
        return bool(re.search(pattern, text, re.I))

    tasks = [{"id": "premises_and_coverage", "instruction":
              "逐问列出实际使用的前提和结论，核对定理条件、量词顺序、阈值、端点、首项及标量域。"
              "参数范围和适用条件也是交付内容；条件小问先核对条件是否成立。"
              "严格与非严格不等式须在等号处代入定义核对分支。"
              "正文中的编号、参数范围和条件贯穿摘要及结论，充分大编号不能升级为全编号恒等式。"
              "映射声明核对定义域、值域、集合所属空间及原像方向。"
              "新增解释也须有定义或有效推导，不得擅自增加属性；保留有效候选论证，接受其他有效路线。"}]
    if matches(r"Hilbert|内积|內積|inner[ -]product|正交|orthogon"):
        scalar_scope = _scalar_scope(text)
        real_check = "从定义核对线性性、对称性及内积展开，写出范数估计所需的中间等式或不等式。"
        complex_check = ("明确固定内积的线性变量及共轭对称约定，坐标式逐项从定义展开。"
                         "选含非零虚部的参数，分别从定义和待核等式计算两边并比较；"
                         "实数样本不足以排除共轭错误，不能只写已核查。"
                         "复内积的范数展开使用实部，排序及 liminf 仅作用于适当实值量。")
        instruction = (real_check if scalar_scope == "real" else complex_check if scalar_scope == "complex"
                       else "先依据题面确认标量域；未指定时不能自行排除复数情形。实数情形："
                       + real_check + "复数情形：" + complex_check)
        tasks.append({"id": "inner_product", "scalar_scope": scalar_scope, "instruction": instruction})
    if category == "测度积分" or matches(
            r"可测|測度|测度|Egorov|Lebesgue|measur|almost\s+everywhere|(?<![a-z])a\.?\s*e\.?(?![a-z])"):
        tasks.append({"id": "measurability", "instruction":
                      "按原题核对可测性与空间完备性，不额外假定极限可测。"
                      "区分已知收敛与待证收敛；已有可测事件足够时直接使用，不添加未经论证的代表存在分支。"
                      "a.e. 例外应由可测零测包络覆盖，实际坏点集可能不可测，不能直接取其测度。"
                      "确需代表时给出构造，在整个包络上固定赋值并证明可测；恢复原函数时将包络并入例外集。"
                      "代表的存在不能以待证收敛为依据，避免循环。"
                      "对实际使用的测度连续性或收敛定理写出全部前提和统一阈值。"})
    if matches(r"尾积分|尾部|一致可积|截断|tail\s+integral|uniform\s+integrab|truncat|Vitali"):
        tasks.append({"id": "tails_and_truncation", "instruction":
                      "变换后收敛重新论证；不连续截断须处理不连续点或阈值集合。"
                      "积分拆分逐项写出不等式，不能由集合包含直接删除交叉项。"
                      "核对常数、各项阈值及共同阈值，先固定阈值取序列极限，再改变阈值。"})
    if category == "拓扑学" or matches(
            r"拓扑|同胚|开集|闭集|topolog|homeomorph|Hausdorff|quotient|open\s+set|closed\s+set"):
        instruction = ("写清映射的定义域和值域、集合所属空间及原像所属空间，核对映射与逆映射的方向。"
                       "在题设空间中由定义或开集原像核对开闭性、连续性及集合关系，写出推论方向与所需前提。"
                       "单射、满射、开闭映射等属性分别从定义或已证明事实推出，不能由另一个性质或集合等式擅自增加。")
        if matches(r"商拓扑|商空间|quotient"):
            instruction += "商空间的开闭集须由商映射原像核对，不凭集合名称猜测。"
        tasks.append({"id": "topology", "instruction": instruction})
    if matches(r"开球|闭球|边界|端点|Baire|一致有界|open\s+ball|closed\s+ball|boundary|endpoint|uniform\s+bounded"):
        tasks.append({"id": "boundaries", "instruction":
                      "成员关系从精确的不等式推出，开球需严格小于半径。"
                      "边界点须独立论证包含或改取严格球内点；核对缩放和零向量。"
                      "反驳成员关系时检查全部已有条件；闭性、对称取点或线性性可能提供额外依据，"
                      "不能仅凭未直接给出就声称不成立。"})
    if matches(r"反例|counter[ -]?example|是否足够|是否必要|disprove|去掉|删去|移除.{0,80}条件|remove\s+.*assumption|"
               r"(?:给出|构造|寻找|举出).{0,160}(?:失败|违反|不成立|但|不满足|不一致|无界)|"
               r"(?:give|construct|find|exhibit).{0,160}(?:fail|violat|but|unbounded|not\b)"):
        tasks.append({"id": "counterexample", "instruction":
                      "列出反例对象并逐项验证全部假设和目标的失败。"
                      "区分每个编号、充分大编号和无穷多个编号，检查首项与端点。"
                      "否定条件断言须给出满足全部前提的反例或有效矛盾推导；理由不足保留未决。"})
    return tasks


def candidate_proof_text(state):
    """Use parsed proof only, never answer summaries, raw drafts or past attempts."""
    result = state.get("reasoning_result") or {}
    if result.get("format_failure"):
        return ""
    return result.get("proof_body") or "\n".join(
        step.get("description", "") for step in result.get("steps", [])
        if isinstance(step, dict))


def _selection_text(candidate):
    text = str(candidate or "")
    if contains_cot_marker(text) or re.search(
            r"^\s*(?:Thinking Process|Thought Process|思考过程|推理过程|分析过程)\s*(?:[：:]|\n|$)", text, re.I):
        return ""
    # Selection ignores quoted templates and self-reports. Handoff stays intact.
    text = re.sub(r"<!--.*?(?:-->|$)", "", text, flags=re.S)
    text = re.sub(r"(?ms)^\s*```[^\n]*\n.*?(?:^\s*```[^\n]*(?:\n|$)|\Z)", "", text)
    text = re.sub(r"(?m)^\s*>.*$", "", text)
    text = re.sub(r"(?m)^\s*(?:已检查|已核查|自检记录|核查记录|review\s+status|checks?\s+completed)[^\n]*$", "", text, flags=re.I)
    return text.strip()


def targeted_proof_checks(problem, category="", candidate_proof=""):
    """Candidate structures can add methods, never replace problem premises.

    The two-argument interface is retained. Fixed task ids bound selection;
    provenance and scalar scopes are advisory, not correctness evidence.
    """
    tasks = _method_tasks(problem, category)
    for task in tasks:
        task["sources"] = ["problem"]
    candidate = _selection_text(candidate_proof)
    by_id = {task["id"]: task for task in tasks}
    for extra in _method_tasks(candidate) if candidate else []:
        if extra["id"] == "premises_and_coverage":
            continue
        existing = by_id.get(extra["id"])
        if existing is None:
            extra["sources"] = ["candidate_proof"]
            tasks.append(extra)
            by_id[extra["id"]] = extra
        else:
            existing["sources"].append("candidate_proof")
            if extra["instruction"] not in existing["instruction"]:
                existing["instruction"] += " 候选结构还需核查：" + extra["instruction"]
        if extra["id"] == "inner_product":
            task = by_id[extra["id"]]
            problem_scope = _scalar_scope(problem)
            candidate_scope = extra["scalar_scope"]
            task.update(problem_scalar_scope=problem_scope, candidate_scalar_scope=candidate_scope,
                        scope_conflict=problem_scope in {"real", "complex"}
                        and candidate_scope in {"real", "complex"} and problem_scope != candidate_scope)
            task["scalar_scope"] = ("complex" if "complex" in {problem_scope, candidate_scope}
                                    else problem_scope if problem_scope != "unspecified" else candidate_scope)
            if task["scalar_scope"] == "complex" and "非零虚部" not in task["instruction"]:
                complex_method = next(item for item in _method_tasks("complex inner product")
                                      if item["id"] == "inner_product")
                task["instruction"] += " 题面标量域还需核查：" + complex_method["instruction"]
            task["instruction"] += (" 题面的标量域保持为前提；候选的标量域仅描述待核局部构造，"
                                     "不得扩充或缩小原题范围；若不一致，核对该构造是否满足题设。")
    conflict = any(task.get("scope_conflict") for task in tasks)
    return {"tasks": tasks, "selection_uncertain": len(tasks) == 1 or conflict,
            "candidate_body_available": bool(candidate_proof),
            "candidate_selection_uncertain": not bool(candidate) or conflict,
            "selection_note": "题面确定前提与问项；候选正文只追加实际使用结构的核查方法，不能认证正确或覆盖题设。"
                              "来源和标量域只是选择线索；未可靠识别的内容仍须按通用规则核查。",
            "mathematical_correctness_verified": False}


def proof_visible_text(raw):
    """Do not mistake a quoted template inside a draft for a formal answer."""
    text = str(raw or "")
    if not contains_cot_marker(text):
        return strip_cot_prefix(text)
    # Drafts often quote the full output template, including fenced headings.
    outside_examples = re.sub(r"(?ms)^\s*```[^\n]*\n.*?(?:^\s*```[^\n]*(?:\n|$)|\Z)", "", text)
    heading = re.search(r"(?m)^ {0,3}## (?:结论速览|问题分析|详细解题步骤|最终答案|关键验证点)\s*$", outside_examples)
    return outside_examples[heading.start():].strip() if heading else ""


def proof_handoff(state):
    result = state.get("reasoning_result") or {}
    raw = state.get("reasoning_raw_response") or ""
    body = result.get("proof_body") or "\n".join(
        step.get("description", "") for step in result.get("steps", []))
    attempts = result.get("proof_attempts", [])
    visible = proof_visible_text(raw)
    # Attempt records already contain the selected full response once.
    if any(item.get("visible_response") == visible for item in attempts):
        visible = "见 attempts 中的完整响应"
    return json.dumps({"proof_body": body, "visible_response": visible,
                       "attempts": attempts,
                       "coverage": result.get("coverage"),
                       "finish_reason": result.get("finish_reason", getattr(raw, "finish_reason", None)),
                       "missing_sections": result.get("missing_sections", []),
                       "note": "结论存在或有一条步骤不代表证明完整；各次片段均须核查，不能凭篇幅选择正确答案；草稿的原始响应另存诊断"}, ensure_ascii=False)


def extract_review(raw):
    """Remove the optional machine note from the public mathematical answer."""
    pattern = r"<!-- PROOF_REVIEW\s*(.*?)-->"
    match = re.search(pattern, str(raw), flags=re.S)
    review = {"status": "unknown", "mathematical_correctness_verified": False}
    if match:
        try:
            data = json.loads(match.group(1))
            if not isinstance(data, dict):
                raise TypeError("Proof review record must be a JSON object")
            if data.get("status") in {"none_reported", "issues_corrected", "unresolved"}:
                review["status"] = data["status"]
                issues = data.get("issues", [])
                review["issues"] = issues if isinstance(issues, list) else []
            if "checks" in data:
                checks = data["checks"]
                review["checks"] = checks if isinstance(checks, list) else []
                review["checks_are_model_reports"] = True
                if any(isinstance(item, dict) and item.get("status") == "unresolved"
                       for item in review["checks"]):
                    review["model_reported_status"] = review["status"]
                    review["status"] = "unresolved"
        except (ValueError, TypeError, AttributeError) as exc:
            review["record_parse_error"] = "invalid_json_or_shape"
            review["record_parse_error_detail"] = str(exc)
            review["raw_record"] = match.group(1)
    else:
        partial = re.search(r"<!-- PROOF_REVIEW\s*(.*)\Z", str(raw), flags=re.S)
        if partial:
            review["record_parse_error"] = "unterminated_record"
            review["raw_record"] = partial.group(1)
    return re.sub(r"<!-- PROOF_REVIEW.*?(?:-->|$)", "", str(raw), flags=re.S).strip(), review
