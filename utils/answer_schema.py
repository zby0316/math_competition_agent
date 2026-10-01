# -*- coding: utf-8 -*-
"""题面字段契约：检查最终答案是否覆盖题目显式要求的全部组成部分。

曾出现这几类残缺：假设检验只给统计量不给结论、置信区间只给单侧端点、运输问题
只给总运费、枚举题只列一个对象、比较题不给建议、矩阵法不展示中间矩阵。

契约只做"是否残缺"的轻量判定，供仲裁、答案选择与 formatter 回捞使用。
仲裁会据此改选候选，因此新增规则必须能从题面明确判断，并且只比较已存在答案。
"""
import re


def _has(text, *patterns):
    return any(re.search(p, text or "") for p in patterns)


def _ci_satisfied(answer):
    text = answer or ""
    # [9.03, 11.97] / (a, b) 之类的成对端点
    if re.search(r"[\[\(]\s*-?\d[^\[\]\(\)，,;；]{0,60}[,，][^\[\]\(\)]{0,60}\d\s*[\]\)]", text):
        return True
    if "下限" in text and "上限" in text:
        return True
    if "±" in text or "\\pm" in text:
        return True
    return False


def answer_part_count(answer):
    """答案包含的独立分量数：按分号/换行分隔的非空段。"""
    parts = re.split(r"[;；\n]+", (answer or "").strip())
    return len([s for s in parts if s.strip()])


def _enumeration_satisfied(answer):
    text = answer or ""
    if answer_part_count(text) >= 2:
        return True
    return len(re.findall(r"(?m)(?:^|[；;]\s*)\s*\d+\s*[.、)]", text)) >= 2


_NUMERIC_OR_SYMBOLIC_RE = re.compile(r"\d|\\frac|\\sqrt|\\pi|π|\\infty")


def _needs_integration_constant(problem):
    """Recognize a single indefinite integral, leaving bounded integrals alone."""
    text = problem or ""
    if re.search(r"(?i)\b(?:indefinite integral|antiderivative)\b|不定积分|原函数", text):
        return True
    integrals = list(re.finditer(r"\\int\b|∫", text))
    if len(integrals) != 1:
        return False
    tail = text[integrals[0].end():]
    if re.match(r"\s*(?:\\limits\s*)?[_^]", tail):
        return False
    if re.search(r"(?i)\b(?:definite integral|from .{0,40} to .{0,40})\b|定积分", text):
        return False
    return True


def _integration_constant_satisfied(answer):
    text = answer or ""
    return _has(
        text,
        r"(?:\+\s*(?:C|K)|(?:C|K)\s*\+)(?:_\{?\d+\}?)?(?![A-Za-z])",
        r"(?i)arbitrary\s+(?:integration\s+)?constant|constant\s+of\s+integration",
        r"积分常数|任意常数",
    )


def _needs_periodic_solution(problem):
    text = problem or ""
    if _has(text, r"\\int\b|∫|\b(?i:integral|antiderivative)\b|积分|原函数"):
        return False
    if not _has(text, r"(?i)\bsolve\b", r"解方程|求(?:解|通解)"):
        return False
    if not _has(text, r"\\?(?:sin|cos|tan|cot)\b|\\?(?:sec|csc)\b|正弦|余弦|正切"):
        return False
    # A restricted interval asks for its solutions, not an unrestricted family.
    if _has(text, r"(?i)\b(?:on|within|over|in)\s+(?:the\s+)?(?:interval|range)\b",
            r"区间|范围|\\in\s*[\[\(]|∈\s*[\[\(]",
            r"(?i)\b[a-z]+\s+in\s*[\[\(]",
            r"0\s*(?:\\leq?|≤|<=|<)\s*(?:\\?[A-Za-z]+)"):
        return False
    return True


def _periodic_solution_satisfied(answer):
    text = answer or ""
    if _has(text, r"(?i)\\pmod\s*\{?2?\\pi|\bmod\s*2?\s*(?:\\pi|π|pi)"):
        return True
    has_pi = _has(text, r"\\pi|π|\b(?i:pi)\b")
    has_parameter = _has(text, r"(?<![A-Za-z])(?:k|n|m|j)(?![A-Za-z])")
    return has_pi and has_parameter


def _requested_answer_fields(problem):
    """Extract only bare function labels from explicit numbered subquestions."""
    text = problem or ""
    clauses = re.findall(r"(?ms)^\s*\d+[.)]\s*(.*?)(?=^\s*\d+[.)]\s*|\Z)", text)
    if len(clauses) < 2:
        return []
    fields = []
    for clause in clauses:
        if not re.match(r"(?i)\s*(?:construct|find|compute|calculate|determine|evaluate|give|求|计算|构造|写出)", clause):
            continue
        spans = re.findall(r"\$([^$]+)\$", clause)
        if len(spans) != 1:
            continue
        label = re.sub(r"\s+", "", spans[0])
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*\([^()]*\)", label):
            fields.append(label)
    return list(dict.fromkeys(fields)) if len(fields) >= 2 else []


def _answer_has_field(answer, field):
    # Match a named result such as R(x,y)=...; mentioning R(x,y) alone is not a result.
    parts = re.match(r"([A-Za-z][A-Za-z0-9_]*)\(([^()]*)\)", field)
    if not parts:
        return False
    name, args = parts.groups()
    arg_pattern = r"\s*,\s*".join(re.escape(arg) for arg in args.split(","))
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(name)}\s*\(\s*{arg_pattern}\s*\)\s*\$?\s*="
    for match in re.finditer(pattern, answer or ""):
        value = re.split(r"[;；\n]", (answer or "")[match.end():], maxsplit=1)[0].strip(" $ ")
        if value and value not in {"?", "...", "\\cdots"}:
            return True
    return False


def _extremum_satisfied(answer):
    """最值问题必须同时交付最优点与最值本身。

    曾出现：答案只给了切线 `y=\\frac{1}{2}x+\\frac{1}{2}`，却把题面作为核心的
    最小面积 `2-\\frac{4\\sqrt{2}}{3}` 丢了。要求最值在位置之外显式出现，
    正好能拦截这类残缺。
    """
    text = answer or ""
    if not _NUMERIC_OR_SYMBOLIC_RE.search(text):
        return False
    # 显式命名的最值（"最大值为 …"、"最小面积 = …"）直接满足条件。
    if re.search(r"(?:最大值|最小值|最值|极大值|极小值|最大|最小)[^。\n]{0,12}"
                 r"(?:为|是|=|等于)", text):
        return True
    # 否则要求至少两个独立量：最优点（如 a=1，或切线方程）与最值本身。
    return len(re.findall(r"=", text)) >= 2 or answer_part_count(text) >= 2


def _tangent_and_area_satisfied(answer):
    text = answer or ""
    has_line = _has(text, r"y\s*=", r"切线|法线", r"x\s*[-+].{0,20}y")
    has_area = _has(text, r"面积|S\s*=") or answer_part_count(text) >= 2
    return has_line and has_area


def _feasibility_count_satisfied(answer):
    """可行性 + 计数双问项：答案必须带一个具体数量。

    曾出现："Is it possible to cover ...? If it is possible, what is the minimum
    number needed?" 的交付是光秃秃的"不可能"——由一个错误的整除性论证得出。
    题面把计数写成第二问，说明出题人预期可行；答案只有否定词而无数量时判为残缺，
    让更完整的候选（通常是 Python 分支或协调器正文）有机会胜出。
    """
    text = (answer or "").strip()
    if not text:
        return False
    if not re.search(r"\d", text):
        return False
    # "不可能，因为 3 不整除 2025" 这类否定+数字仍然没有回答"多少个"。
    negation_only = re.match(
        r"^\s*(?:不可能|不能|无法|impossible|no\b|not possible)", text, flags=re.I)
    return not negation_only


_SATISFIED = {
    "hypothesis_conclusion": lambda a: _has(a, r"拒绝|接受原?假设|显著差异|不显著|无显著|显著地?(?:高|低|大|小|不同)"),
    "ci_two_sided": _ci_satisfied,
    "allocation": lambda a: len(re.findall(r"x_?\{?\d{2}\}?\s*=", a or "")) >= 2,
    "comparison_advice": lambda a: _has(a, r"建议|选择|更优|优于|较优|更好"),
    "matrices_shown": lambda a: _has(a, r"X\^T\s*X|X\^\{?T\}?\s*X|\\begin\{[pbv]matrix\}|Matrix\("),
    "enumerate_all": _enumeration_satisfied,
    "extremum_value": _extremum_satisfied,
    "tangent_and_area": _tangent_and_area_satisfied,
    "feasibility_count": _feasibility_count_satisfied,
    "integration_constant": _integration_constant_satisfied,
    "periodic_solution": _periodic_solution_satisfied,
}


def required_components(problem):
    """由题面文本推断答案必须包含的组成部分（component id 列表）。"""
    p = problem or ""
    comps = []
    if _needs_integration_constant(p):
        comps.append("integration_constant")
    if _needs_periodic_solution(p):
        comps.append("periodic_solution")
    comps.extend("field:" + field for field in _requested_answer_fields(p))
    if "检验" in p and _has(p, r"H_?\{?0\}?|原假设|显著性水平|\\alpha|α"):
        comps.append("hypothesis_conclusion")
    if "置信区间" in p and not _has(p, r"单侧|置信上限|置信下限"):
        comps.append("ci_two_sided")
    if _has(p, r"运输问题|最小元素法|西北角法|伏格尔|Vogel") and _has(p, r"初始|基可行解"):
        comps.append("allocation")
    if "比较" in p and _has(p, r"建议|选择|哪个|何者|更优|优劣"):
        comps.append("comparison_advice")
    if _has(p, r"矩阵方法|矩阵运算|用矩阵|写出.{0,8}矩阵") and _has(p, r"回归|OLS|最小二乘"):
        comps.append("matrices_shown")
    if _has(p, r"(?:确定|求出?|列出|写出|枚举|找出|给出)[^。\n]{0,16}所有") or "互不同构" in p:
        comps.append("enumerate_all")
    else:
        # 英文题面的 "Find all the possible values of ..." 不匹配上面的中文模式，
        # 曾交付三个值里的一个。复用题型画像的双语判定。
        from utils.question_profile import requires_all_solutions

        if requires_all_solutions(p):
            comps.append("enumerate_all")
    # 最值问题：只有最优点不是答案，最值本身才是。
    # （曾出现：只给使面积最小的切线，却丢了最小面积。）
    if _has(p,
            r"最大值|最小值|最值|极大值|极小值|取得最大|取得最小",
            # "…图形的面积最小"：量词在"最"字之前、从句可很长，因此锚定量词本身。
            r"(?:面积|体积|周长|长度|距离|用量|成本|费用|误差|值)\s*最(?:大|小)",
            r"使[^。\n]{0,40}最(?:大|小)"):
        comps.append("extremum_value")
    if _has(p, r"切线|法线") and _has(p, r"面积"):
        comps.append("tangent_and_area")
    from utils.question_profile import asks_feasibility_then_count

    if asks_feasibility_then_count(p):
        comps.append("feasibility_count")
    return comps


def missing_components(problem, answer):
    """返回答案缺失的契约组成部分；空列表表示满足契约。"""
    return [
        component for component in required_components(problem)
        if not (_answer_has_field(answer, component[6:]) if component.startswith("field:")
                else _SATISFIED[component](answer or ""))
    ]


def recover_hypothesis_conclusion(fulltext):
    """从完整解题说明中回捞假设检验的结论句（拒绝/不拒绝 + 实际含义）。

    条件句（"若…则拒绝…"）不是结论，需带 因此/所以/故/由于/结论 等引导词，
    或本身不含条件词。取最后一个匹配句（结论通常在文末）。
    """
    sents = re.split(r"[。\n]", fulltext or "")
    best = ""
    for s in sents:
        s = s.strip().strip("*# ")
        if not s or len(s) > 160:
            continue
        if not re.search(r"不拒绝|拒绝|接受", s):
            continue
        if not re.search(r"原?假设|H_?\{?[01]\}?|显著", s):
            continue
        conditional = re.search(r"若|如果|否则|一旦", s)
        conclusive = re.search(r"因此|所以|故|由于|结论|综上", s)
        if conditional and not conclusive:
            continue
        best = s
    return best


def recover_confidence_interval(fulltext):
    """从完整解题说明中回捞形如 [9.03, 11.97] 的置信区间（取最后一个）。"""
    best = ""
    for m in re.finditer(r"[\[\(]\s*-?\d[^\[\]\(\)，,;；]{0,60}[,，][^\[\]\(\)]{0,60}\d\s*[\]\)]",
                         fulltext or ""):
        best = m.group(0)
    return best
