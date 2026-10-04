"""从技能文档里选取与题目相关的部分，而不是只取开头。

各节点把技能文档截断在几千字符（推理分支是 ``skill_document[:3000]``）。这个
上限是位置性的：比上限长的文档，无论题目问什么，都只会静默给出开头那些模块。

这曾是个真实缺陷——当"非基础及进阶课程"从 7.9k 涨到 8.9k 字符时，它的数论与
博弈论模块落在偏移 3404-8949 处，整个超出 3000 字符窗口；结果一道博弈论题目
拿到的是 Lebesgue 测度与泛函分析模块，而真正为它写的内容一个字都没进。

选段依据 ``## 模块`` 或 ``### 模块`` 标题，按与题面的术语重合度打分。文档自带的开头前言
始终保留，剩余预算按文档顺序分给最匹配的模块。没有任何模块命中时，退化成
原来的头部截断，因此没有模块标题的文档不受影响。
"""

from __future__ import annotations

import re

#: 这类文档的模块边界：要么是传统的 ``## 模块...`` 标题，
#: 要么是竞赛技能手册使用的编号式 ``## 1. ...``。
_MODULE_RE = re.compile(r"^#{2,3}\s*(?:模块|\d+\s*[.)、:：-])", re.MULTILINE)
#: 末尾的通用小节，与领域无关、对任何题目都适用。
_UNIVERSAL_HEADING_RE = re.compile(r"^#{2,3}\s*(?:跨学科|通用|附录)", re.MULTILINE)
#: 中文连续段与拉丁单词；单个汉字太常见，不足以用来区分。
_TERM_RE = re.compile(r"[一-鿿]{2,}|[A-Za-z][A-Za-z\-]{2,}")

#: 题面语言判定：拉丁字母与 CJK 字数。
_CJK_CHAR_RE = re.compile(r"[一-鿿]")
_LATIN_CHAR_RE = re.compile(r"[A-Za-z]")


def _is_english_dominant(problem: str) -> bool:
    """题面是否以英文为主。

    中文手册的模块标题无法与英文题面做字面重合（``_score`` 是词汇交集），因此
    正文模块对英文题面恒得 0 分；而"模块速查"尾巴里的 ``- 检索词：`` 行塞了
    how many / minimum / subsets 之类的英文泛词，会被 ``_retrieval_bonus`` 白
    给分。结果是英文题面**必然**由速查尾巴霸榜、把正文模块整体挤出预算。

    实测（修复前）：一道 "How many derangements of 5 elements are there?"
    在 85914 字符的《离散数学》手册上只抽到 2803 字符，命中的是"鸽巢原理在整数
    组极值中的应用""序列求和约束的极值保证与构造""网格最坏情形极值"三条无关
    速查，而真正对口的正文模块"组合计数"得 0 分、从未进入 prompt。

    仓库两套公开题集（U-MATH / UGMathBench）全部是英文题面，故该路径是常态而非
    边角。英文题面下停用速查尾巴的检索词加成，让选段退化为确定性的头部截断
    （手册总纲 + 首批正文模块），而不是注入无关条目。
    """
    text = problem or ""
    latin = len(_LATIN_CHAR_RE.findall(text))
    cjk = len(_CJK_CHAR_RE.findall(text))
    return latin >= 12 and latin > 3 * cjk

#: 模块标题表达不出的主题线索。博弈论题目会说 游戏/必胜/策略，却从不写
#: 博弈论 这个词，纯术语重合会把为它写的模块和无关模块打成同分。
#: 每个条目把"模块标题关键词"映射到"应当把该模块拉进来的题面词汇"。
_TOPIC_CUES = {
    "内积": ("内积", "复数", "complex", "inner product", "Hilbert"),
    "弱收敛": ("弱收敛", "rightharpoonup", "weak", "Hilbert"),
    "一致有界": ("一致有界", "逐点有界", "Baire", "uniform boundedness",
                 "pointwise bounded", "Banach-Steinhaus"),
    "博弈": ("游戏", "必胜", "策略", "先手", "后手", "轮流", "获胜", "对手", "取石子",
             "双人", "玩家", "甲", "乙",
             # 英文竞赛题面同类线索（term 交集打分吃不到，此处补齐）。
             "game", "player", "players", "opponent", "wins", "winning",
             "turns", "alternately", "strategy"),
    "数论": ("整除", "同余", "素数", "质数", "互素", "模", "gcd", "最大公约", "幂次",
             "正整数", "因子", "倍数",
             # 只收判别力强的英文词——integer/number 之类的泛词会把数论模块
             # 错误地拉进一半的英文题面。
             "prime", "divisible", "divides", "modulo", "coprime"),
}


def _cue_bonus(module: str, problem: str) -> int:
    """按题面里出现的领域线索给模块加权。

    单模块封顶 9 分：博弈类速查条目共享"博弈"标题，线索加成不封顶时同族条目
    会集体霸榜，把真正对口但标题不带关键词的条目挤出预算。
    """
    heading = module.split("\n", 1)[0]
    text = (problem or "").casefold()
    bonus = 0
    for key, cues in _TOPIC_CUES.items():
        if key in heading:
            bonus += 3 * sum(1 for cue in cues if cue.casefold() in text)
    return min(bonus, 9)


#: 速查条目携带的显式检索行。中文题面是长 CJK 连续串，_TERM_RE 的"最大连续段"词元
#: 对它几乎必然失配（"概率不超过" ≠ "概率"），所以检索行按**子串**语义单独记分：
#: 行内每个词元（中文或 ≥4 字符拉丁词）在题面中出现即 +2，短拉丁词按词边界匹配。
#: 上限封顶，避免一行通用词把真正的领域模块挤出预算。
_RETRIEVAL_LINE_RE = re.compile(r"^-\s*检索词[：:](.+)$", re.MULTILINE)


def _retrieval_bonus(module: str, problem: str) -> int:
    match = _RETRIEVAL_LINE_RE.search(module)
    if not match:
        return 0
    text = (problem or "").casefold()
    hits = 0
    for token in match.group(1).split():
        tok = token.casefold().strip()
        if len(tok) < 2:
            continue
        if re.fullmatch(r"[a-z][a-z\-]*", tok):
            if len(tok) >= 4:
                hit = tok in text
            else:
                hit = bool(re.search(rf"(?<![a-z]){re.escape(tok)}(?![a-z])", text))
        else:
            hit = tok in text
        if hit:
            hits += 1
            if hits >= 6:
                break
    # 单词元命中只记 1 分：一个泛词不足以断言主题相关，不得压过正文模块的
    # 实词匹配（同分时文档序在前的正文模块优先）。多词元命中才给满权重。
    return hits if hits < 2 else 2 * hits


def _split_modules(document: str, boundary=_MODULE_RE) -> tuple[str, list[str]]:
    """返回 (前言, 各小节)。没有匹配到任何边界时 `sections` 为空。"""
    starts = [match.start() for match in boundary.finditer(document)]
    if not starts:
        return document, []
    bounds = starts + [len(document)]
    modules = [document[bounds[i]:bounds[i + 1]] for i in range(len(starts))]
    return document[:starts[0]], modules


def _terms(text: str) -> set[str]:
    return {match.group(0).lower() for match in _TERM_RE.finditer(text or "")}


def _score(module: str, problem_terms: set[str]) -> int:
    """模块词汇与题面词汇的重合度。

    标题计双倍：标题叫 博弈论 的模块，对一道博弈题的信号强度远大于
    其他模块里偶然提到一次 博弈。
    """
    heading = module.split("\n", 1)[0]
    module_terms = _terms(module)
    heading_terms = _terms(heading)
    return len(module_terms & problem_terms) + 2 * len(heading_terms & problem_terms)


#: 验证示例虽然历史文件名是 ``.py``，本质是提示词文档。知识点标题是主要边界；
#: 保留旧的横幅形式，让带有老提示词资产的知识库仍能拿到有用的摘录。
_SCRIPT_SECTION_RE = re.compile(r"^(?:##\s+|#\s*(?:=====|-----))", re.MULTILINE)


def select_script_excerpt(script: str, problem: str, limit: int) -> str:
    """给提示词式验证参考用的 `select_skill_excerpt`。

    裸 ``[:2000]`` 仍可能把长领域提示词里的相关知识点藏住，所以改为
    选取词汇与题目匹配的小节。
    """
    return _select(script, problem, limit, _SCRIPT_SECTION_RE)


def select_skill_excerpt(document: str, problem: str, limit: int) -> str:
    """至多返回 `document` 的 `limit` 字符，偏向 `problem` 的主题。

    文档没有模块结构或全部零分时退化为 `document[:limit]`，
    因此可以无痛替换裸切片，且不改变那类场景的行为。
    """
    return _select(document, problem, limit, _MODULE_RE)


def _select(document: str, problem: str, limit: int, boundary) -> str:
    text = document or ""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text

    preamble, modules = _split_modules(text, boundary)
    if not modules:
        return text[:limit]

    problem_terms = _terms(problem)
    # 英文题面下必须把 `- 检索词：` 行从打分文本里剔除。那些行是给中文题面写的
    # 线索，行内的英文泛词（how many / minimum / subsets）会被 _terms(module)
    # 收进来**直接命中英文题面**，让无关的速查条目拿到正分。实测一道
    # "How many derangements of 5 elements are there?" 让
    # "模块速查：网格最坏情形极值：识别查询与覆盖统计" 拿到 _score=2，
    # 而真正对口的正文模块 "## 2. 组合计数" 得 0 分被挤出预算。
    english = _is_english_dominant(problem)
    if english:
        scored = [(index, _score(_RETRIEVAL_LINE_RE.sub("", module), problem_terms)
                   + _cue_bonus(module, problem))
                  for index, module in enumerate(modules)]
    else:
        scored = [(index, _score(module, problem_terms) + _cue_bonus(module, problem)
                   + _retrieval_bonus(module, problem))
                  for index, module in enumerate(modules)]
    if not any(score for _, score in scored):
        return text[:limit]

    # 通用小节（跨学科通用方法论）对任何题目都适用，因此与其他模块同场竞争，
    # 而不是被领域打分单独挤出预算。
    for index, module in enumerate(modules):
        if _UNIVERSAL_HEADING_RE.match(module):
            scored[index] = (index, scored[index][1] + 1)

    ranked = [pair for pair in sorted(scored, key=lambda pair: (-pair[1], pair[0]))
              if pair[1] > 0]
    # 整册无模块结构、仅在文末追加了速查条目的手册：preamble 是全部正文，本身就
    # 超过预算。此时头部（手册的总纲与核心方法）与命中的速查模块平分预算，谁都
    # 不独占。有模块结构的手册 preamble 均远小于预算，走不进这个分支。
    if len(preamble) >= limit:
        # 头部封顶 1200：整册头部再长，也要给命中的第 2、3 名条目留全量空间
        # （排序配对条目曾差 9 字符被挤出）。
        head = preamble[: min(limit // 2, 1200)]
        pieces, room = [], limit - len(head)
        for rank_pos, (index, _) in enumerate(ranked):
            if room <= 60:
                break
            module = modules[index]
            if len(module) > room:
                # 只有榜首允许截断；次名截断会剪掉条目尾部的收尾配方，宁可跳过
                # 换一个能整条放下的。
                if rank_pos == 0:
                    pieces.append((index, module[:room]))
                    room = 0
                continue
            pieces.append((index, module))
            room -= len(module)
        return head + "".join(piece for _, piece in sorted(pieces))
    # 得分最高的模块先拿预算，必要时截断到能放下。若按文档序贪心填充，
    # 会塞进两个不相关的小模块，却给真正为本题写的那个模块留不出空间——
    # 这正是整个模块要防止的失败。
    best = ranked[0][0]
    room = max(0, limit - len(preamble))
    if len(modules[best]) > room:
        return (preamble + modules[best][:room]) if room else modules[best][:limit]

    kept = [best]
    used = len(preamble) + len(modules[best])
    for index, _ in ranked[1:]:
        length = len(modules[index])
        if used + length > limit:
            continue
        kept.append(index)
        used += length

    parts = [preamble] + [modules[index] for index in sorted(kept)]
    excerpt = "".join(parts)
    return excerpt if len(excerpt) <= limit else excerpt[:limit]
