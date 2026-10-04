"""题型画像与客观题快速路径。

题面通常不会显式携带 ``type`` 字段，且官方题集同时包含证明、计算、选择、
判断和填空。把题型识别放在一个小的、无副作用的模块里，避免每个节点各自用
不一致的关键词判断，尤其避免把“判断题”误当成证明题。
"""

from __future__ import annotations

import re
from typing import Any, Mapping


KNOWN_MODES = frozenset({"proof", "computation", "choice", "true_false", "fill"})
CLOSED_MODES = frozenset({"choice", "true_false", "fill"})

_TYPE_TO_MODE = {
    "计算": "computation", "计算题": "computation", "computation": "computation",
    "proof": "proof", "证明": "proof", "证明题": "proof",
    "choice": "choice", "选择": "choice", "选择题": "choice", "单选": "choice", "多选": "choice",
    "fill": "fill", "填空": "fill", "填空题": "fill",
    "true_false": "true_false", "判断": "true_false", "判断题": "true_false",
}

# 选项标记既出现在行首，也可能内嵌在题干中间（官方题集原文即如此）。
_OPTION_RE = re.compile(r"(?<![A-Za-z0-9])[A-HＡ-Ｈ]\s*[.．、:：)）]")
_OPTION_PAREN_RE = re.compile(r"(?<![A-Za-z0-9])[（(]\s*[A-HＡ-Ｈ]\s*[）)]")

# ---- 选择题二次裁决（防误判）----
# 曾出现："设 (A)(B) 是两矩阵求 det(AB) 最大值" 因 (A)(B) 标签命中选项判据被
# 判成选择题，模型只答一个选项字母、整题丢分——(A)(B) 是矩阵名不是选项。
_CHOICE_STEM_RE = re.compile(r"下列|正确的是|错误的是|不正确|选项|选出|符合题意|单选|多选")
_DOT_LABEL_RE = re.compile(r"(?<![A-Za-z0-9])[A-HＡ-Ｈ]\s*[.．、]")
_PAREN_LABEL_RE = re.compile(r"[（(]\s*[A-HＡ-Ｈ]\s*[）)]")
_RIGHTPAREN_LABEL_RE = re.compile(r"(?<![A-Za-z0-9（(])[A-HＡ-Ｈ]\s*[)）]")

#: 逐字母扫描选项的上限长度：超过它的文本按"解答/散文"处理，不再从中捞字母。
#: 真正的选项串都极短（"A"、"A、C、F"、"1. A；2. C"、"答案为 A、B、F"），
#: 而英文解答里孤立的 f/g/h/a 会被逐字母扫描误认成选项。
_CHOICE_MAX_SCAN_CHARS = 60
_SOLVING_VERB_RE = re.compile(r"求|计算|证明|化简|求解|判断|最大值|最小值|最大|最小")


def _label_letters(text: str, pattern) -> set:
    """取某个标签正则命中处的字母集合（全角归一、去重、大写）。"""
    letters = set()
    for match in pattern.finditer(text):
        found = re.search(r"[A-HＡ-Ｈ]", match.group(0))
        if found:
            letters.add(found.group(0).translate(_FULLWIDTH_TRANSLATION).upper())
    return letters


def _looks_like_option_list(text: str) -> bool:
    """标签是否真的构成一份选项列表。

    **真选项列表从 A 开始。** 把字母表放宽到 A–H 之后，英文题面里的变量加句点
    （``Simplify F. 2 and G. 3 expressions.``）会被 ``_DOT_LABEL_RE`` 数成两个标签，
    把计算题误判成选择题——而单选题模式会强制模型只回一个选项字母，答案直接作废。
    实测（放宽字母表前）：4 个英文计算题全判 ``computation``；放宽后全变 ``choice``。

    括号族仍按原设计要求 ≥ 3 项（选项列表通常至少三项）。
    """
    dot = _label_letters(text, _DOT_LABEL_RE)
    if len(dot) >= 2 and min(dot) == "A":
        return True
    paren = _label_letters(text, _PAREN_LABEL_RE) | _label_letters(
        text, _RIGHTPAREN_LABEL_RE)
    return len(paren) >= 3 and min(paren) == "A"


def _looks_like_real_choice(text: str) -> bool:
    """对已命中选项判据的题面做二次裁决：是否真的构成选择题。

    强信号直接判选择题：选择题主干标记（下列/正确的是/选项/单选…）。
    其余情形**必须先构成选项列表**（见 ``_looks_like_option_list``）：
      * 仅 2 个括号族标签且含求解动词、无主干标记时判不是选择题（假设条件贴标签
        的典型形态，如"设 (A)(B) 是两矩阵求 det(AB) 最大值"）；
      * 标签不从 A 开始时判不是选择题（英文题面里的 F./G./H. 是变量不是选项）。
    """
    if _CHOICE_STEM_RE.search(text):
        return True
    if not _looks_like_option_list(text):
        return False
    if _SOLVING_VERB_RE.search(text):
        return False
    return True
_TRUE_FALSE_RE = re.compile(
    r"(?:^|[\n。；;])\s*(?:判断|正误|对错)\s*(?:题)?\s*[:：]?|"
    r"(?:判断题|正误题|对错题|正确与否|命题是否正确|说法是否正确|"
    r"(?:判断|判定)(?:下列|该|此)?(?:命题|说法|结论))",
    re.I,
)
_FILL_RE = re.compile(
    r"填空|填“|填\"|填入|_{3,}|＿{2,}|[(（]\s*(?:\\\s*)?[)）]"
)
_PROOF_RE = re.compile(
    r"(?:^|[\n])\s*(?:证明|试证)|(?:^|[\n])\s*(?:prove|show)\b|"
    r"证明题|(?<!or )\b(?:prove|show|demonstrate|establish|justify)\s+that\b|"
    r"\b(?:prove|show|demonstrate|establish|justify)\b",
    re.I,
)

#: 强证明信号：即使题面带 (A)(B) 这类假设标签、或含选项字母，只要出现这些
#: 显式"求证/论证"词，就优先判为证明题而非选择题。曾实测：复分析题把假设
#: 条件标成 (A)(B)，被选项检测误判成 choice，模型答了个 "A"。
_STRONG_PROOF_RE = re.compile(
    r"(?i)\b(?:show|prove|demonstrate|establish|justify|verify|derive)\b"
    r"\s+(?:that|the|for|any|every|there|a|an|\()|"
    r"证明|求证|试证|论证|推导|说明理由|给出证明|argument\s+principle|辐角原理|"
    r"residue\s+theorem|留数定理|laurent\s+expansion",
)


def mode_from_metadata(metadata: Mapping[str, Any] | None) -> str:
    """读取下发方提供的题型字段，但不把它变成运行时依赖。

    公开题面输入通常只有 ``problem`` 与 ``idx`` 两个字段；部分接入方还会额外
    下发 ``type``/``question_mode``。优先采纳这些字段可以消除不必要的歧义，
    纯文本判型路径保持原样不动。
    """
    if not isinstance(metadata, Mapping):
        return ""
    for key in ("question_mode", "type", "question_type", "problem_type"):
        value = str(metadata.get(key, "") or "").strip().lower()
        if value in KNOWN_MODES:
            return value
        if value in _TYPE_TO_MODE:
            return _TYPE_TO_MODE[value]
    return ""


def classify_question_mode(problem: str) -> str:
    """从题面原文返回一个稳定的题型名。

    客观题标记要先于证明词检查：选择题干里可能含"判断下列命题"，
    但仍应走短小的客观题路径。
    """

    text = str(problem or "").strip()
    if not text:
        return "computation"
    if _TRUE_FALSE_RE.search(text) or re.search(r"判断\s*(?:题)?\s*[：:]", text):
        return "true_false"
    # 强证明信号优先于选项检测：题面用 (A)(B) 标注假设条件、或含单个选项字母时，
    # 只要有显式"show/prove/证明/求证/argument principle"就判为证明题，
    # 避免被下面的 choice 检测抢走（曾实测复分析论证题被误判成选择题）。
    if _STRONG_PROOF_RE.search(text):
        return "proof"
    # 官方题面常在题干后用 ``（）``，并把选项内嵌（``A.均值B.方差``）。
    # 选项检测必须先于通用空位标记，否则几乎每道客观题都会被判成填空。
    option_count = len(_OPTION_RE.findall(text)) + len(_OPTION_PAREN_RE.findall(text))
    if option_count >= 2 or re.search(r"(?:选择题|单选|多选|下列.*(?:正确|错误|是|为).*选)", text, re.I):
        if _looks_like_real_choice(text):
            return "choice"
        # 二次裁决否决（(A)(B) 是矩阵/对象名而非选项）：继续走常规判型。
    if _FILL_RE.search(text) or re.search(
        r"(?:填空题|fill\s*in|blank|指标是|分别为\s*$)", text, re.I
    ):
        return "fill"
    if _PROOF_RE.search(text):
        return "proof"
    return "computation"


def is_objective_mode(mode: str) -> bool:
    return mode in CLOSED_MODES


# ---- 题面结构探测（按题面语义补充的定向核验） ----

_FILL_SLOT_RE = re.compile(r"_{3,}|＿{2,}|—{3,}|[（(]\s*(?:\\\s*)?[）)]")

#: "求所有/找出全部" 型题：解空间可能含多个分支，单一构造不可作答。
_ALL_SOLUTIONS_RE = re.compile(
    r"(?i)find\s+all|determine\s+all|all\s+possible|every\s+possible|"
    r"所有可能|全部(?:的)?(?:解|值|情况|可能)|求所有|找出所有|确定所有|哪些"
)

#: 极值/最优题：需要构造与上/下界双向闭环，且构造须与竞争构造比较。
_EXTREMAL_RE = re.compile(
    r"(?i)最大|最小|最多|最少|至多|至少|最优|最短|最长|极值|"
    r"\b(?:maximum|minimum|maximal|minimal|largest|smallest|greatest|least|"
    r"at\s+most|at\s+least|optimal)\b"
)

#: 值域为二元域/模 m 结构的函数声明（f: Q → F_2 等）。
_MOD_CODOMAIN_RE = re.compile(
    r"(?:\\rightarrow|\\to|→|->)\s*\\?(?:mathbb\s*\{?\s*F\s*\}?_\{?2\}?|F_?2\b|"
    r"GF\(2\)|\\?mathbb\s*\{?\s*Z\s*\}?\s*/\s*2)"
)

#: 同名函数值相加（f(a)+f(b)）——和落在函数值域所在的代数结构里。
_FUNC_SUM_RE = re.compile(r"([a-zA-Z])\s*\([^()]{1,60}\)\s*\+\s*\1\s*\(")

#: 开区间/严格不等式限定的取值范围。"between a and b" 是竞赛惯用的开区间表述；
#: 由 ≤ 推出的界在严格范围内取不到，临界值必须逐一核验可达性。
_OPEN_BOUND_RE = re.compile(
    r"(?i)\bbetween\b[^,.;]{1,40}\band\b|strictly\s+(?:between|less|greater)|"
    r"开区间|严格(?:小于|大于|介于|单调)|"
    # |x| < 1 型严格不等式（排除 ≤/\le/\leq）
    r"(?<![\\])<(?!=)"
)

#: "第 k 大/第 k 小/第 k 个" 型序号问法：通项与序号极易差一。
#: 序数词与数字之间常隔着 LaTeX 包裹（``$9$th``、``{10}th``），必须允许。
_ORDINAL_RANK_RE = re.compile(
    r"(?i)\b\d+\s*[$}\s]*(?:st|nd|rd|th)\b[^.\n]{0,40}"
    r"\b(?:largest|smallest|greatest|least|biggest|term|element|number|value)\b|"
    r"第\s*\$?\s*[0-9一二三四五六七八九十k]+\s*\$?\s*(?:大|小|个|项|位)"
)

#: 可行性 + 计数的双问项（"是否可能…若可能，最少需要多少"）。
_FEASIBILITY_RE = re.compile(
    r"(?i)\bis it possible\b|\bcan (?:we|you|one|it)\b|\bdoes there exist\b|"
    r"是否(?:可能|存在|能)|能否|可否"
)
_COUNT_ASK_RE = re.compile(
    r"(?i)\b(?:minimum|maximum|smallest|largest|fewest|greatest)\s+number\b|"
    r"\bhow many\b|最少(?:需要)?(?:多少|几)|最多(?:能|可以)?(?:多少|几)|"
    r"需要多少|数量是多少"
)

#: 求范围/区间/取值集合：答案必须显式给出区间端点或集合，而非单个边界值。
#: 曾实测：解析几何题"determine all positive real numbers t"答案是 0<t≤4，
#: 模型只报了上界 4，丢掉整个区间。
_RANGE_ASK_RE = re.compile(
    r"(?i)\b(?:range|interval|values? of|all (?:positive )?real numbers?)\b|"
    r"取值范围|值域|求范围|的范围|区间|范围是|的取值范围"
)


def has_open_interval_bound(problem: str) -> bool:
    """题面用开区间/严格不等式限定取值（端点不可达，临界值需 +1 核验）。"""
    return bool(_OPEN_BOUND_RE.search(str(problem or "")))


def asks_ordinal_rank(problem: str) -> bool:
    """题面问"第 k 大/小/个"（序号与通项必须逐项对齐核验）。"""
    return bool(_ORDINAL_RANK_RE.search(str(problem or "")))


def asks_feasibility_then_count(problem: str) -> bool:
    """题面同时问"是否可能"与"最少/最多需要多少"（不可能性论证须先证伪）。"""
    text = str(problem or "")
    return bool(_FEASIBILITY_RE.search(text)) and bool(_COUNT_ASK_RE.search(text))


def count_blanks(problem: str) -> int:
    """题面可见空位数（填空题输出契约用）。检测不到显式空位记号时返回 0。

    实测发现：三空只答零空、置信 0.78 直接放行——缺的就是这个计数。
    """
    return len(_FILL_SLOT_RE.findall(str(problem or "")))


def fill_part_count(answer: str) -> int:
    """填空答案里的分项数（按分号切分）。"""
    text = str(answer or "").strip()
    if not text:
        return 0
    return len([part for part in re.split(r"[；;]", text) if part.strip()])


def requires_all_solutions(problem: str) -> bool:
    """题面是否要求"所有解/全部值"（须触发解空间枚举，不得单支作答）。"""
    return bool(_ALL_SOLUTIONS_RE.search(str(problem or "")))


def asks_range(problem: str) -> bool:
    """题面是否求取值范围/区间（答案须为完整区间/集合，不得只报单边界）。"""
    return bool(_RANGE_ASK_RE.search(str(problem or "")))


def is_extremal_problem(problem: str) -> bool:
    """题面是否为极值/最优化问题（构造须与竞争构造对照，防自洽陷阱）。"""
    return bool(_EXTREMAL_RE.search(str(problem or "")))


def has_mod2_valued_sum(problem: str) -> bool:
    """函数值域为 F₂/模 2 且所求为函数值之和（和必须在该结构内聚合）。

    曾发现：六个 f 值全对，最后按整数相加得 3；题面 f:Q→F₂，
    和应在 F₂ 中为 1。通用口径清单不足以扭转，这里做题面条件触发的定向注入。
    """
    text = str(problem or "")
    return bool(_MOD_CODOMAIN_RE.search(text)) and bool(_FUNC_SUM_RE.search(text))


def mode_instruction(mode: str) -> str:
    """推理/Python 节点共用的提示词后缀。

    这里刻意要求给出精确的交付物而不是猜测的答案；既保持客观题简洁，
    又为解析器保留完整的四段式输出契约。
    """

    if mode == "choice":
        return (
            "\n\n[题型画像：选择题快速路径]\n"
            "先判断单选还是多选：题面注明'多选/不定项'或多个选项各自独立成立时按多选，"
            "否则倾向单选。逐项按教材定义的**严格措辞**核对：概念被替换、方向说反、"
            "表述不精确的选项一律不选，宁缺勿滥。最后在'最终答案'中明确写出全部正确选项"
            "（如 A、C），不要把选项说明误当成答案。最多 3 个短步骤。"
        )
    if mode == "true_false":
        return (
            "\n\n[题型画像：判断题快速路径]\n"
            "先给出‘正确’或‘错误/不正确’，再用一个反例或定理说明理由；不要把条件句当作最终判断。"
        )
    if mode == "fill":
        return (
            "\n\n[题型画像：填空题快速路径]\n"
            "按空位顺序逐项作答，答案行格式：空1: <结果>；空2: <结果>（只有一个空时直接给结果）。"
            "每个空必须填教材术语/具体数值/明确方向（如'有偏''低估或高估'），"
            "禁止填'不确定/视情况而定'；不要输出空位数量本身或任何操作说明，"
            "不要把一个空的同义解释拆成多个空，不输出与空位无关的长篇探索。"
        )
    if mode == "proof":
        return (
            "\n\n[题型画像：证明题]\n"
            "必须写出从假设到结论的关键蕴含链，至少列出两步，并检查边界条件/等号条件。"
        )
    return (
        "\n\n[题型画像：计算题]\n"
        "先列出题面要求的全部量，再计算并在最终答案中逐项标注；不要用一个中间量代替最终结论。"
        "若题目要求‘所有/确定集合/轨迹/最小或最大常数’，必须同时给出候选的必要性与充分性（或上界与构造）"
        "并核对边界，不能只报一个猜测值。"
    )


def structure_instruction(problem: str) -> str:
    """按题面结构追加的强制步骤，推理与 Python 分支共用。

    与 mode_instruction 互补：mode 看题型，这里看题面语义（全部解/极值）。
    检测不到相应结构时返回 ""，不增加提示词负担。
    """
    parts: list[str] = []
    if has_mod2_valued_sum(problem):
        parts.append(
            "[值域聚合警告] 题面函数的值域是 F_2（模 2 的二元域）。所求的 f(⋯)+f(⋯)+… 是"
            " **F_2 中的加法**：先逐项求出各函数值（0 或 1），最后必须按模 2（异或）聚合，"
            "最终答案只能是 0 或 1；给出普通整数和（如 3）即错。"
        )
    if requires_all_solutions(problem):
        parts.append(
            "[全部解检查单] 本题要求给出**所有**解/值。禁止只验证一个已知解就作答："
            "必须先在小规模/截断版本上系统枚举解空间（列出发现的每一个解支），"
            "再证明再无其他分支；最终答案必须列出全部解支（含平凡支与例外支）。"
        )
    if asks_range(problem) and not requires_all_solutions(problem):
        parts.append(
            "[取值范围检查单] 本题求取值范围/区间。最终答案必须写成**完整的区间或集合**"
            "（如 a<t≤b、[a,b]、{t | 条件}），并逐条核验端点可达性："
            "由 ≤ 推出的界取得到，由严格不等式推出的界取不到。"
            "禁止只报单个边界值或单个最值。"
        )
    if is_extremal_problem(problem):
        parts.append(
            "[极值题对照检查] 本题求极值/最优。单一构造族内的自洽验证不构成最优性证明："
            "必须 (1) 用小规模精确解（暴力/DP）校准；(2) 至少比较两个结构不同的构造，"
            "取更优者；(3) 给出与构造值匹配的上界/下界论证。三者缺一即在验证点中声明未完成。"
        )
    # 以下三条针对实测中的错因：口径与序号类错误都发生在"推理跑完了"之后，
    # 属于收尾核验缺失，成本极低但直接决定得分。
    if has_open_interval_bound(problem) and is_extremal_problem(problem):
        parts.append(
            "[端点可达性检查] 题面用开区间/严格不等式限定取值（如 between a and b、"
            "|S|<1）。由非严格不等式（≤）推出的界在严格范围内**取不到**："
            "推出 n ≥ K 之后必须对 n=K 显式写出**全部变量的具体数值或构造式**，"
            "并把它们逐条代回原式检验每一个等式与每一个严格不等号；"
            "禁止用“可以构造”“取 n=K 即可”一类断言代替检验。"
            "若检验发现只有让某个变量取到端点（或让某个严格不等式取等）才能成立，"
            "则 n=K 不可行，答案是 K+1，并在验证点中写出使等号无法成立的那一步。"
        )
    if asks_ordinal_rank(problem):
        parts.append(
            "[序号对齐检查] 本题问“第 k 大/第 k 小/第 k 个”。禁止直接写通项作答："
            "必须先显式列出 k=1、2、3 对应的前三个对象（写出具体表达式或数值），"
            "确认序号从 1 起数且与通项逐项对齐，再代入题面的 k；"
            "最终答案与该列表必须自洽（差一即错）。"
        )
    if asks_feasibility_then_count(problem):
        parts.append(
            "[不可能性论证的证伪要求] 本题先问可行性、再问所需数量。"
            "断言“不可能/不存在”之前必须完成两项证伪：(1) 在缩小规模的同类实例上"
            "（如把边长换成 3、5、7）实际构造或暴力搜索，只要小规模可行，"
            "不可能性论证即有误；(2) 复核不变量论证的每一步——同一形状/操作在染色或"
            "计数下的贡献若不唯一（例如一块拼图可覆盖 2 黑 1 白，也可覆盖 1 黑 2 白），"
            "总量整除性就**不构成**不可能性证明。若可行，最终答案必须给出具体数量，"
            "不得只回答“可能/不可能”。"
        )
    if not parts:
        return ""
    return "\n\n" + "\n".join(parts)


def answer_coverage_clause(problem: str) -> str:
    """压缩重试专用：必须保全的**答案覆盖度**（不是推导篇幅）。

    实测发现：截断后压缩重试的指令写着"沿最可行的一条路线、每章从简"，
    模型据此只报了三个可能值中的一个。压缩针对的是推导过程，
    答案该覆盖的分支一个都不能少——这句提醒随压缩指令一起发出。
    """
    clauses = []
    if requires_all_solutions(problem):
        clauses.append(
            "题目要求“所有可能的值/全部解”：压缩的是推导篇幅，不是解的个数——"
            "'## 最终答案' 必须把每一个解支都列出来（写成集合），"
            "只报一支按缺解计零分"
        )
    if asks_feasibility_then_count(problem):
        clauses.append("题目同时问可行性与数量：最终答案必须给出具体数量，不能只答可能/不可能")
    if is_multi_ask(problem):
        clauses.append("题目含多个问项：每一问都要在最终答案中单独给出结果")
    if not clauses:
        return ""
    return "\n**答案覆盖度要求（不受“从简”约束）**：" + "；".join(clauses) + "。"


def is_multi_ask(problem: str) -> bool:
    """题面是否有多个并列问项（复用答案抽取侧的多问项判定）。"""
    from utils.answer_pull import is_multi_part_problem

    return is_multi_part_problem(problem)


_FULLWIDTH_TRANSLATION = str.maketrans("ＡＢＣＤＥＦＧＨａｂｃｄｅｆｇｈ", "ABCDEFGHabcdefgh")


def _strip_answer_wrappers(answer: str) -> str:
    text = str(answer or "").strip()
    # 优先取最后一个显式标签。回答可能先引了一句中间的 ``答案：...``，再在
    # 结尾补一个正式结论；取最后一个标签可避免把推导理由误当成提交内容。
    labelled = list(re.finditer(
        r"(?:最终答案|答案|结论|选择)\s*[：:]\s*([^\n]+)", text, flags=re.I
    ))
    if labelled:
        text = labelled[-1].group(1).strip()
    else:
        text = re.sub(r"^\s*(?:最终答案|答案|结论|选择)\s*[：:]\s*", "", text, flags=re.I)
    # 保留最后一个方框表达式：客观题回答可能在理由里先引一个选项，
    # 最后才落到答案行。
    boxed = re.findall(r"\\boxed\s*\{([^{}]*)\}", text, flags=re.S)
    if boxed:
        text = boxed[-1].strip()
    # 简洁客观题契约把理由放在分号或 ``依据/理由`` 标签之后；选择/判断题
    # 需要剥掉这部分。填空答案保留内部的分号，因为它可能表示多个空位。
    text = re.split(r"(?:\n|[；;])\s*(?:依据|理由|说明)\s*[：:]", text, maxsplit=1)[0]
    return text.strip().strip("。．.;；\"'“”")


def normalize_objective_answer(answer: str, mode: str) -> str:
    """把短的选择/判断/填空答案规范化，不做求解。

    这里刻意只做格式整理：剥掉包装，选择题按出现顺序保留选项字母，
    绝不从解释文字反推缺失的选项。
    """
    text = _strip_answer_wrappers(answer)
    if not text:
        return ""
    if mode == "choice":
        text = text.translate(_FULLWIDTH_TRANSLATION)
        # 连写形态（"BD"、"\boxed{BD}" 剥壳后）：字母间无分隔符时逐字母展开。
        # 单词边界正则对连写必然失配（B 的后瞻撞上 D），\boxed{BD} 曾因此
        # 归一化为空。仅限 2-8 个纯 A-H 字母的短串，避免把普通英文单词错当选项串。
        # 上限放到 8：官方题库存在 A-F 六选项多选（ugmath_hard26 idx 9/21），
        # 全选 "ABCDEF" 这种连写串在上限 5 时会被整串清空而丢掉答案。
        if re.fullmatch(r"[A-H](?:\s*[A-H]){1,7}", text, flags=re.I):
            letters = re.findall(r"[A-H]", text, flags=re.I)
        elif len(text) <= _CHOICE_MAX_SCAN_CHARS:
            letters = re.findall(r"(?<![A-Za-z0-9])[A-H](?![A-Za-z0-9])", text, flags=re.I)
        else:
            # 长文本不是选项串：英文解答里的孤立 f、g、h、a（"for $f$ near $a$
            # with $h=0.1$"）会被逐字母扫描误当选项。差分验证实测（同一批输入
            # 喂原始版与补丁版）：
            #   "1. The average rate of change for $f$ near $a$ ..." -> 'F、A、H'
            #   proof_gap12 两道证明长文 -> 'E、F' 与 'F、C、G、A'
            # 这类答案交给判分器看原文，远好过交一串凭空多出来的字母。
            letters = []
        ordered = []
        for letter in letters:
            upper = letter.upper()
            if upper not in ordered:
                ordered.append(upper)
        return "、".join(ordered)
    if mode == "true_false":
        # 优先识别"不正确/错误"这类双字否定短语，再落到单独的 ``正确``。
        matches = list(re.finditer(r"不正确|错误|正确|不对|对|错|×|√", text, flags=re.I))
        if not matches:
            return ""
        # 第一个判断词才是答案；后面的出现通常属于解释句
        # （例如 ``错误，因为...正确``）。
        value = matches[0].group(0)
        return "正确" if value in {"正确", "对", "√"} else "错误"
    return text


def objective_answer_is_usable(answer: str, mode: str) -> bool:
    normalized = normalize_objective_answer(answer, mode)
    if not normalized:
        return False
    if mode == "choice":
        return bool(re.fullmatch(r"[A-H](?:、[A-H])*", normalized))
    if mode == "true_false":
        return normalized in {"正确", "错误"}
    if mode == "fill":
        # 无章节的长篇 CoT 可能被散文捞回逻辑误当成填空答案（例如一句以公式
        # 结尾的英文散句）。拒掉明显的叙事文本，让有界预填重试有机会输出所
        # 要求的空位内容；数学表达式与多空位的教材式短语仍然放行。
        from utils.answer_sanitize import is_junk
        from utils.answer_pull import looks_incomplete_answer

        if looks_incomplete_answer(normalized):
            return False
        # 元指令回显 / 思维碎片（如 "Count blanks: 1"）与非答案术语
        # （如 "不确定"）都不是可提交的填空内容。
        if is_junk(normalized):
            return False
        if re.fullmatch(r"不确定|不一定|视情况(?:而定)?|无法判断", normalized):
            return False
        # 带"空N:"前缀的不确定类回答（如 "空1: 不确定"）：
        # 逐空剥掉前缀后检查；全部空位都是不承诺答案时整体拒收，混合时保留部分得分。
        parts = [re.sub(r"^\s*空\s*\d+\s*[:：]\s*", "", part).strip()
                 for part in re.split(r"[；;]", normalized)]
        parts = [part for part in parts if part]
        if parts and all(
            re.fullmatch(r"不确定|不一定|视情况(?:而定)?|无法判断|无法确定|不能确定", part)
            for part in parts
        ):
            return False
        # 填空答案是术语/数值/短表达式；成段英文散句只可能是泄漏的思考文本
        # （答题纠结的独白曾以 fill 答案形态出厂）。
        # LaTeX 表达式的词数远低于此阈值（\frac/\partial 等 2-3 词）。
        if len(re.findall(r"[A-Za-z]{2,}", normalized)) > 6:
            return False
        if len(normalized) > 420 or "\n" in normalized or "?" in normalized or "？" in normalized:
            return False
        if re.search(
            r"\b(?:let's|consider|under|because|therefore|we need|the phrase|actually|however)\b"
            r"|(?:如果|是否|请判断|请问|无法确定|不能确定)[^；;。]*[？?]?$",
            normalized,
            flags=re.I,
        ):
            return False
    return not re.fullmatch(r"(?:答案|结果|待求|无法确定|不能确定)(?:[：:].*)?", normalized)


def fill_answer_matches_blanks(answer: str, problem: str) -> bool:
    """填空答案的分项数是否覆盖题面的全部空位（空位 ≤1 时恒真）。

    填空题必须"校验空数一致"。检测不到显式空位记号时不设限，
    以免误伤没有下划线记号的填空题。
    """
    blanks = count_blanks(problem)
    if blanks <= 1:
        return True
    return fill_part_count(answer) >= blanks
