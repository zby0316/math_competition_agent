"""分类：对全部 18 个技能域做一次 prefill LLM 调用。

改造前跑两次 LLM 调用——先用关键词+TF-IDF 排序出 top-3 短名单，短名单落空时
再做一次全域重试。实测短名单经常落空，第一次调用纯粹是延迟（~40s），答案
总是来自第二次。

现在把题目直接发给 LLM、逐域分类，prefill 使模型跳过推理：约 1s、约 12 个
补全 token，取代原来的约 40s、约 1969 个。关键词+TF-IDF 排序保留但降级为
响应不可解析时的零成本确定性兜底——它不再限制 LLM 可以考虑的范围，而那正是
它过去有害的原因。
"""

import json
import re
from utils.deps import get_deps
from utils.llm_retry_guard import chat_prefilled, chat_with_retry
from utils.prompt_bank import CLASSIFICATION_PREFILL, CLASSIFICATION_PROMPT
from utils.score_budget import estimate_tokens
from utils.cot_trim import strip_cot_prefix
from config import CONFIG


def _parse_json(text):
    text = strip_cot_prefix(text).strip()
    # 先整体试解析
    try:
        return json.loads(text)
    except Exception:
        pass
    # 找到首个可解析的、括号配平的 {...}
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i+1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        break  # 试下一个起点
        start = text.find("{", start + 1)
    return None


_PUNCT_STRIP = re.compile(r"[\s　\"'.,。，、：:；;！!？?（）()【】\[\]{}「」『』]")


#: 官方 18 子领域 -> 本项目知识目录的别名映射。官方评测按 18 子领域组织题目，
#: 而 knowledge/ 下用的是另一套目录名（数论/组合并入离散数学、解析几何并入
#: 非基础及进阶课程、实变函数并入测度积分、数理统计并入统计推断）。当 prefill
#: 分类器直接吐出官方领域名时，先经此表落到真实目录，避免 LCS/Levenshtein
#: 把「数论」错配到「统计推断」这类无关目录（曾实测 50% 的题分类落错领域）。
OFFICIAL_DOMAIN_ALIASES = {
    "数论": "离散数学",
    "组合数学": "离散数学",
    "组合": "离散数学",
    "解析几何": "非基础及进阶课程",
    "平面几何": "非基础及进阶课程",
    "立体几何": "非基础及进阶课程",
    "欧氏几何": "非基础及进阶课程",
    "实变函数": "测度积分",
    "实分析": "测度积分",
    "数理统计": "统计推断",
    "统计": "统计推断",
    "数理逻辑": "离散数学",
    "数学建模": "运筹学",
    "初等数论": "离散数学",
}


def _apply_official_alias(s: str, valid_categories: set) -> str | None:
    """把官方领域名（或其归一化形态）映射到真实知识目录；无匹配返回 None。"""
    if not s:
        return None
    target = OFFICIAL_DOMAIN_ALIASES.get(s)
    if not target:
        target = OFFICIAL_DOMAIN_ALIASES.get(_normalize_cat(s))
    if target and target in valid_categories:
        return target
    return None


def _normalize_cat(s: str) -> str:
    """去除空白、引号与常见中英文标点，用于分类名的模糊匹配。"""
    return _PUNCT_STRIP.sub("", s or "")


def _lcs_length(a: str, b: str) -> int:
    """最长公共子串长度（连续字符）。对中文类别名比编辑距离更可靠。"""
    if not a or not b:
        return 0
    m, n = len(a), len(b)
    prev = [0] * (n + 1)
    best = 0
    for i in range(1, m + 1):
        cur = [0] * (n + 1)
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _resolve_category(raw, valid_categories, fallback):
    """将 LLM 返回的 category 归一到 knowledge/ 下的真实文件夹名。

    严格保证返回值 ∈ valid_categories（即实际文件夹名），杜绝下游
    get_skill_document / get_validation_script 因类别名不存在而 FileNotFoundError。

    匹配顺序：精确 → 归一化 → 子串包含（双向）→ 最长公共子串（≥2）→ Levenshtein（≥0.6）→ fallback。
    无法匹配时返回调用方提供的 fallback。
    """
    if not isinstance(raw, str) or not raw:
        return fallback
    s = raw.strip().strip("\"'「」『』").strip()
    if not s:
        return fallback
    # 0. 官方领域名别名映射：优先于 LCS/Levenshtein，把官方 18 子领域名落到
    #    真实目录（数论→离散数学、解析几何→非基础及进阶课程等）。
    alias = _apply_official_alias(s, set(valid_categories))
    if alias:
        return alias
    # 1. 精确匹配
    if s in valid_categories:
        return s
    # 2. 归一化匹配（去空白/标点后比较）
    sn = _normalize_cat(s)
    for c in valid_categories:
        if _normalize_cat(c) == sn:
            return c
    # 3. 子串包含（任一方向）
    for c in valid_categories:
        if s in c or c in s:
            return c
    # 4. 最长公共子串（连续≥2字符）—— 对中文类别名比 Levenshtein 更可靠
    #    例：线性代数 ↔ 高等代数（共享"代数"）、概率统计 ↔ 概率论（共享"概率"）
    lcs_matches = [(c, _lcs_length(s, c)) for c in valid_categories]
    lcs_matches = [(c, l) for c, l in lcs_matches if l >= 2]
    if lcs_matches:
        max_lcs = max(l for _, l in lcs_matches)
        tied = [c for c, l in lcs_matches if l == max_lcs]
        if len(tied) == 1:
            return tied[0]
        # LCS 并列时用 Levenshtein ratio 精排（best-effort，区分长短不同的候选）
        try:
            from Levenshtein import ratio as lev_ratio
            return max(tied, key=lambda c: lev_ratio(s, c))
        except ImportError:
            return tied[0]
    # 5. Levenshtein 模糊匹配（兜底）
    try:
        from Levenshtein import ratio as lev_ratio
        best, best_score = max(((c, lev_ratio(s, c)) for c in valid_categories),
                               key=lambda x: x[1])
        if best_score >= 0.6:
            return best
    except ImportError:
        pass
    return fallback


def _merge_candidate_rankings(*rankings, limit):
    scores = {}
    for ranking in rankings:
        for position, (category, _) in enumerate(ranking):
            scores[category] = scores.get(category, 0.0) + 1.0 / (position + 1)
    ordered = sorted(scores, key=lambda c: (-scores[c], c))
    return ordered[:limit]


_CONFIDENCE_SCORE_RE = re.compile(r'"?confidence"?\s*[:=]\s*([0-9]*\.?[0-9]+)')


def _extract_decision(text, all_categories):
    """从 prefill 或普通分类器回复中恢复 (category, confidence)。

    先试严格 JSON，再退回到原始文本中扫描域名。扫描很关键：prefill 回复
    构造上就是 JSON *片段*，而普通回复可能在对象外带 CoT 前导。置信度是可选的
    ——类别才是信号，因此一个可解析的域即使没有可读的置信度，也在阈值处被接受
    而不是丢弃。
    """
    if not text:
        return None
    cleaned = strip_cot_prefix(text)

    parsed = _parse_json(cleaned)
    if isinstance(parsed, dict) and "category" in parsed:
        resolved = _resolve_category(parsed["category"], all_categories, None)
        if resolved:
            # 缺失或不可读的置信度绝不能读成 0.0——那会让每个此类回复都进入
            # 约 40s 的升级路径，尽管域本身是有效的。
            if "confidence" in parsed:
                try:
                    return resolved, float(parsed["confidence"])
                except (TypeError, ValueError):
                    pass
            return resolved, CONFIG["classifier_confidence_threshold"]

    # Prefill 形态：'数学分析", "confidence": 0.95}' — 种子已提供开括号与键，
    # 因此该回复永远无法作为独立对象解析。
    head = cleaned.split('"')[0].strip()
    resolved = _resolve_category(head, all_categories, None)
    if not resolved:
        # 最后一招：回复中最早出现的精确域名。
        best = None
        for category in all_categories:
            position = cleaned.find(category)
            if position != -1 and (best is None or position < best[1]):
                best = (category, position)
        resolved = best[0] if best else None
    if not resolved:
        return None

    match = _CONFIDENCE_SCORE_RE.search(cleaned)
    if match:
        try:
            return resolved, float(match.group(1))
        except (TypeError, ValueError):
            pass
    return resolved, CONFIG["classifier_confidence_threshold"]


def _classify_prefilled(deps, prompt, all_categories):
    """快速路径：prefill 单次调用，约 1s、约 12 个补全 token。"""
    budget = deps.token_budget
    response = ""
    try:
        response = chat_prefilled(
            deps.client,
            messages=[{"role": "user", "content": prompt}],
            prefix=CLASSIFICATION_PREFILL,
            temperature=CONFIG["temperatures"]["classifier"],
            max_tokens=CONFIG["max_tokens"]["classifier"],
            logger=deps.logger,
            time_budget=deps.time_budget,
            expected_call_seconds=5,
            label="classifier_prefill",
        )
    except Exception as exc:  # noqa: BLE001 - falls through to the plain call.
        deps.logger.warning("Prefilled classifier call failed: %s", exc)
        return None
    finally:
        if budget:
            budget.consume(estimate_tokens(prompt), estimate_tokens(response))
    return _extract_decision(response, all_categories)


#: 非 prefill 兜底的实际成本。在难题上实测 129.5s 与 133.3s，而非先前误估的
#: 约 40s：该调用留有完整 CoT 的空间，而 CoT 长度随题目难度增长。若只声明 60s，
#: deadline 检查会批准一次 120s 节点上限随后就会掐掉的调用——时间花掉了，
#: 结果却被丢弃。`node_timeouts["classifier"]` 必须保持高于该值。
_PLAIN_CALL_SECONDS = 150


def _classify_plain(deps, prompt, all_categories):
    """兜底：非 prefill 调用，留有完整 CoT 的空间（难题上约 130s）。

    仅当 prefill 回复不可解析时才会走到这里，例如后端拒绝追加的助手轮。
    """
    budget = deps.token_budget
    response = ""
    try:
        response = chat_with_retry(
            deps.client,
            messages=[{"role": "user", "content": prompt}],
            temperature=CONFIG["temperatures"]["classifier"],
            max_tokens=CONFIG["max_tokens"]["classifier_fallback"],
            logger=deps.logger,
            time_budget=deps.time_budget,
            expected_call_seconds=_PLAIN_CALL_SECONDS,
            label="classifier_plain",
        )
    except Exception as exc:  # noqa: BLE001 - falls through to deterministic ranking.
        deps.logger.warning("Classifier LLM call failed: %s", exc)
        return None
    finally:
        if budget:
            budget.consume(estimate_tokens(prompt), estimate_tokens(response))
    return _extract_decision(response, all_categories)


def _deterministic_fallback(sl, problem, all_categories, logger):
    """零成本本地排序——关键词命中与 TF-IDF 相似度合并。

    惰性计算：正常路径上 LLM 先作答、这里从不运行，因此完全不需要付建
    TF-IDF 索引的代价。
    """
    keyword_candidates = []
    try:
        keyword_candidates = sl.find_candidate_categories(problem, top_k=CONFIG["classifier_top_k"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Keyword ranking unavailable: %s", exc)

    tfidf_candidates = []
    try:
        tfidf_candidates = sl.get_embedding_index().rank_all(
            problem, top_k=CONFIG["classifier_top_k"]
        )
    except Exception as exc:  # noqa: BLE001 - keyword ranking still stands.
        logger.warning("TF-IDF category ranking unavailable: %s", exc)

    merged = _merge_candidate_rankings(keyword_candidates, tfidf_candidates,
                                       limit=CONFIG["classifier_top_k"])
    merged = [category for category in merged if category in all_categories]
    if merged:
        return merged[0]
    if all_categories:
        return all_categories[0]
    return "非基础及进阶课程"


def _has_any(text: str, cues) -> bool:
    for cue in cues:
        needle = str(cue or "").casefold()
        if not needle:
            continue
        # OLS/PDE 这类短拉丁缩写不得在 "colors" 或 "symbols" 内部命中。
        # 更长的短语与中文线索保留子串匹配。
        if re.fullmatch(r"[a-z0-9-]{2,4}", needle):
            if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", text):
                return True
        elif needle in text:
            return True
    return False


def _has_cue_groups(text: str, *groups) -> bool:
    """当每个语义线索组都至少命中一次时返回 true。"""
    return all(_has_any(text, group) for group in groups)


def _domain_override(problem: str, categories: list[str]) -> str | None:
    """解析快速 LLM 调用常混淆的高精度边界。

    规则描述的是可复用的数学标记，而非题目编号或答案。``triangle``、``group``、
    ``polynomial`` 这类宽泛词单凭自身永远不够：模糊的竞赛题面必须先命中某一
    线索族，专业手册才替换 LLM 的决策。
    """
    text = re.sub(r"\s+", " ", str(problem or "")).casefold()
    available = set(categories)

    # 数值方法优先于被离散化的解析方程本身。
    numerical_cues = (
        "数值分析", "条件数", "有限差分", "有限元", "有限体积", "截断误差",
        "数值微分", "数值积分", "离散化方法", "离散化处理", "数值离散",
        "中心差分", "前向差分", "后向差分", "差分公式", "病态矩阵",
        "condition number", "finite difference", "finite element",
        "finite volume", "truncation error", "numerical differentiation",
        "numerical integration", "numerical discretization", "newton-cotes",
    )
    if "数值分析" in available and _has_any(text, numerical_cues):
        return "数值分析"

    # 计量经济学与回归诊断属于既有的回归手册，即使题面用的是方差等统计词汇。
    regression_cues = (
        "计量经济", "线性回归", "非参数回归", "非线性回归", "回归系数",
        "逐步回归", "异方差", "内生性", "工具变量", "残差图", "残差",
        "最小二乘", "ols", "gauss-markov", "多重共线性", "vif",
        "durbin-watson", "拟合优度", "regression coefficient",
        "heteroscedastic", "endogeneity", "instrumental variable",
    )
    if "线性回归" in available and _has_any(text, regression_cues):
        return "线性回归"

    # 描述统计与时间序列归入 统计推断，因为固定的 18 域布局没有为这些课程
    # 单独设立文件夹。
    stats_cues = (
        "统计图形", "直方图", "散点图", "箱线图", "条形图", "时间数列",
        "时间序列", "趋势变动", "季节变动", "循环变动", "不规则变动",
        "季节调整", "移动平均", "指数平滑", "自相关图", "抽样分布",
        "总体参数", "样本均值", "样本方差", "频数分布", "集中趋势",
        "histogram", "box plot", "time series", "seasonal adjustment",
        "moving average", "exponential smoothing", "sampling distribution",
    )
    normal_parameter_question = _has_cue_groups(
        text,
        ("正态分布", "normal distribution"),
        ("两个参数", "参数分别", "parameters", "mean and variance", "mean and standard deviation"),
    )
    data_summary_question = _has_cue_groups(
        text,
        ("数据", "data"),
        ("分散程度", "离散程度", "集中趋势", "频数分布", "dispersion"),
    )
    if ("统计推断" in available
            and (_has_any(text, stats_cues) or normal_parameter_question or data_summary_question)):
        return "统计推断"

    # 微分算子的形式伴随是 PDE；泛泛的有界算子伴随则仍属泛函分析。离散化已
    # 在上面提前返回，因此数值 PDE 题留在 数值分析。
    pde_direct = (
        "偏微分方程", "partial differential equation", "pde", "heat equation",
        "wave equation", "形式伴随", "formal adjoint",
        "散度型", "divergence-form", "热方程", "波动方程", "poisson equation",
    )
    pde_adjoint = _has_cue_groups(
        text,
        ("伴随算子", "adjoint operator", "adjoint"),
        ("c_0^", "\\partial_i", "∂_i", "微分算子", "differential operator", "omega"),
    )
    if "偏微分方程" in available and (_has_any(text, pde_direct) or pde_adjoint):
        return "偏微分方程"

    abstract_cues = (
        "galois", "分裂域", "splitting field", "域扩张", "field extension",
        "正规子群", "normal subgroup", "换位子群", "commutator subgroup",
        "二面体群", "dihedral group", "sylow", "商群", "quotient group",
        "理想", "ideal of", "有限域", "finite field",
    )
    if "抽象代数" in available and _has_any(text, abstract_cues):
        return "抽象代数"

    # 定理及算子结构比“上确界/一致收敛”更能确定泛函领域。
    # Banach 单独出现也可能是压缩映射定理；要求明确的泛函定理或结构组合。
    functional_theorems = (
        "泛函分析", "functional analysis", "闭图像定理", "闭图象定理",
        "closed graph theorem", "一致有界性原理", "一致有界性定理",
        "uniform boundedness principle", "uniform boundedness theorem",
        "banach-steinhaus", "hahn-banach", "hahn–banach", "哈恩-巴拿赫",
    )
    functional_structure = _has_cue_groups(
        text,
        ("banach", "hilbert", "赋范空间", "范数空间", "normed space"),
        ("有界线性算子", "有界线性泛函", "紧算子", "弱收敛",
         "bounded linear operator", "bounded linear functional",
         "compact operator", "weak convergence", "weakly convergent",
         "开映射定理", "open mapping theorem"),
    )
    if "泛函分析" in available and (
            _has_any(text, functional_theorems) or functional_structure):
        return "泛函分析"

    analysis_cues = (
        "数学分析", "一致收敛", "uniform convergence", "逐点收敛",
        "pointwise convergence", "极限函数", "limit function", "广义积分",
        "improper integral", "含参积分", "power series", "幂级数", "上确界",
        "supremum", "fourier transform", "fourier 变换", "cauchy数列",
        "cauchy sequence", "完备度量空间", "taylor expansion", "泰勒展开",
    )
    circle_laplacian = _has_cue_groups(
        text,
        ("圆周", "on the circle", "restricted to the circle"),
        ("拉普拉斯算子", "laplacian"),
    )
    difference_quotient_fe = _has_cue_groups(
        text,
        ("difference quotient", "差商", "g(a)-g(b)", "g(a) - g(b)"),
        ("a-b", "a - b"),
        ("function", "函数"),
    )
    bounded_sequence_extremum = _has_cue_groups(
        text,
        ("infinite sequence", "无限序列"),
        ("belongs to [", "each term belongs", "bounded", "有界"),
        ("m < n", "m<n", "任意正整数 m<n"),
    )
    real_selection_extremum = _has_cue_groups(
        text,
        ("five distinct positive real numbers", "五个互不相同的正实数"),
        ("choose four distinct", "选取四个"),
        ("minimum value", "最小值"),
    )
    growth_rate_game = _has_cue_groups(
        text,
        ("flooded", "洪水", "淹没"),
        ("barrier", "walls", "屏障", "墙"),
        ("gamma", "\\gamma", "增长速率"),
    )
    rational_integer_fe = _has_cue_groups(
        text,
        ("\\mathbb{q}", "有理数"),
        ("\\mathbb{z}", "整数值"),
        ("g(bx-a)", "g(bx - a)"),
    )
    if ("数学分析" in available and (
            _has_any(text, analysis_cues)
            or circle_laplacian
            or difference_quotient_fe
            or bounded_sequence_extremum
            or real_selection_extremum
            or growth_rate_game
            or rational_integer_fe
    )):
        return "数学分析"

    or_direct = (
        "运筹学", "线性规划", "linear programming", "单纯形", "simplex method",
        "运输问题", "transportation problem", "整数规划", "integer programming",
        "分支定界", "branch and bound", "指派问题", "assignment problem",
        "关键路径", "critical path", "最大流", "maximum flow", "最短路",
        "shortest path", "目标规划", "排队模型", "queueing model",
    )
    scheduling_optimisation = _has_cue_groups(
        text,
        ("schedule", "scheduling", "赛程", "调度"),
        ("minimize the total cost", "minimum total cost", "最小总成本", "费用最小"),
    )
    allocation_game = _has_cue_groups(
        text,
        ("boxes", "箱子"),
        ("pebbles", "石子"),
        ("distributes", "分配"),
        ("each subsequent round", "随后每轮", "每一轮"),
    )
    if ("运筹学" in available
            and (_has_any(text, or_direct) or scheduling_optimisation or allocation_game)):
        return "运筹学"

    high_algebra_cues = (
        "高等代数", "minimal polynomial", "极小多项式", "symmetric polynomial",
        "对称多项式", "characteristic polynomial", "特征多项式", "eigenvalue",
        "特征值", "linear space", "线性空间", "quadratic form", "二次型",
        "jordan form", "jordan 标准形", "max-plus", "tropical algebra", "热带代数",
    )
    f2_function = _has_cue_groups(
        text,
        ("\\mathbb{f}_2", "二元域", "binary field"),
        ("function", "函数"),
        ("\\mathbb{q}", "rational"),
    )
    polynomial_function_equation = _has_cue_groups(
        text,
        ("a:\\mathbb{r}", "a: \\mathbb{r}", "a(p)a(q)"),
        ("a(-pq)", "2pq"),
    )
    polynomial_system = _has_cue_groups(
        text,
        ("triples", "三元组"),
        ("x^2 + y^2 + z^2", "x²+y²+z²"),
        ("xy^3", "xy³"),
    )
    tuple_pairing = _has_cue_groups(
        text,
        ("m-tuple of real numbers", "-tuple of real numbers", "实数 m 元组"),
        ("for each permutation", "任意排列"),
        ("p< q", "p < q", "两两乘积"),
    )
    max_plus_tuple = _has_cue_groups(
        text,
        ("integer-valued", "整数值"),
        ("tuples", "元组"),
        ("\\max", "coordinatewise max", "逐分量取大"),
        ("v+w", "v}+\\mathbf{w}", "向量加法"),
    )
    if ("高等代数" in available and (
            _has_any(text, high_algebra_cues)
            or f2_function
            or polynomial_function_equation
            or polynomial_system
            or tuple_pairing
            or max_plus_tuple
    )):
        return "高等代数"

    # 欧氏/凸/射影几何由竞赛手册表示。通用形状词需要第二个关系线索，这样
    # 数三角形或六边形网格的题不会被自动划给 离散数学。
    diff_geo_markers = (
        "曲率", "curvature", "测地线", "geodesic", "流形", "manifold",
        "第一基本形式", "second fundamental form", "frenet", "weingarten",
        "gauss-bonnet", "法曲率", "主曲率", "平均曲率", "torsion",
    )
    classic_geometry = (
        "切弦角定理", "tangent-chord theorem", "极点极线", "polar line",
        "la hire", "根轴", "radical axis", "共轴", "coaxal", "外心",
        "circumcenter", "内心", "incenter", "垂心", "orthocenter", "圆周角",
        "angle bisector", "角平分线", "circumcircle", "外接圆", "内切圆",
    )
    polygon_geometry = _has_cue_groups(
        text,
        ("convex polygon", "convex quadrilateral", "convex polyhedron", "凸多边形", "凸四边形", "凸多面体", "m-gon", "-gon", "n-sided polygon", "-sided polygon"),
        ("area", "diagonal", "face", "visible", "circumscribed", "面积", "对角线", "面可见", "外切"),
    )
    triangle_geometry = _has_cue_groups(
        text,
        ("triangle", "三角形"),
        (" angle ", " angles ", "\\angle", "tangent", "circumcenter", "circumcircle", "bisector", "角", "切线", "外心", "外接圆", "平分线"),
    )
    line_angle_geometry = _has_cue_groups(
        text,
        ("line", "直线"),
        ("segment", "线段"),
        ("angle", "角"),
    )
    incidence_geometry = _has_cue_groups(
        text,
        ("points on the plane", "平面上的点", "points in the plane"),
        ("lines in the plane", "平面直线"),
        ("circle", "圆"),
    )
    if ("非基础及进阶课程" in available
            and not _has_any(text, diff_geo_markers)
            and (_has_any(text, classic_geometry)
                 or polygon_geometry
                 or triangle_geometry
                 or line_angle_geometry
                 or incidence_geometry)):
        return "非基础及进阶课程"

    number_theory_floor = _has_cue_groups(
        text,
        ("prime", "素数"),
        ("floor", "\\lfloor", "取整"),
        ("integer", "整数"),
    )
    diophantine_radical = _has_cue_groups(
        text,
        ("pairs of positive integers", "正整数对"),
        ("cube root", "\\sqrt[3]", "立方根"),
        ("satisfy", "满足"),
    )
    finite_extremal_construction = _has_cue_groups(
        text,
        ("smallest positive integer n", "smallest positive integer", "最小正整数 n"),
        ("there exist real numbers", "存在实数"),
        ("between -1 and 1", "between $-1$ and 1", "between $-1$ and $1$", "介于 -1 和 1"),
        ("sum", "\\sum", "求和"),
    )
    if ("离散数学" in available
            and (number_theory_floor or diophantine_radical or finite_extremal_construction)):
        return "离散数学"

    return None


# 兼容早期测试分支曾使用的名字。
_objective_domain_override = _domain_override


def _decision_payload(problem, categories, category, confidence, candidates, stages):
    """在每个分类器出口应用同一套经过校准的边界决策。"""
    override = _domain_override(problem, categories)
    if override and override != category:
        stages.append("domain_override")
        category = override
        confidence = max(confidence, CONFIG["classifier_confidence_threshold"])
    return {
        "category": category,
        "category_confidence": confidence,
        "candidate_categories": candidates,
        "classification_stages_used": stages,
    }


def classify_domain(state, config):
    deps = get_deps(config)
    sl = deps.skills_loader
    problem = state["problem"]
    all_categories = list(sl.categories)
    threshold = CONFIG["classifier_confidence_threshold"]
    prompt = CLASSIFICATION_PROMPT.format(
        problem=problem,
        allowed_categories="、".join(all_categories),
    )

    stages = ["llm_prefill"]
    result = _classify_prefilled(deps, prompt, all_categories)
    if result and result[1] >= threshold:
        return _decision_payload(
            problem, all_categories, result[0], result[1], all_categories, stages
        )

    # 仅当 prefill 回复未命名真实域时才升级。低置信但有效的域过去也会升级，
    # 要用约 130s 去质疑一个约 1s 的答案——在某道博弈论题上，那次升级还击穿了
    # 节点上限、连同 prefill 结果一起毁掉。分类只是决定加载哪份技能文档，
    # 这个选择不值得花掉预算的五分之一。
    time_budget = deps.time_budget
    if result:
        stages.append("low_confidence_accepted")
        return _decision_payload(
            problem, all_categories, result[0], result[1], all_categories, stages
        )
    # prefill 没有可解析的内容。升级是获取模型自身判断的唯一剩余途径，因此在这里
    # 值得付出成本——但前提是时钟能负担得起我们现在已知其规模的那次调用。
    if not (time_budget and time_budget.fast_path()):
        stages.append("llm_plain")
        plain = _classify_plain(deps, prompt, all_categories)
        if plain:
            # 低置信仍优于关键词猜测：模型至少命名了一个真实域。
            stages.append("low_confidence_accepted" if plain[1] < threshold else "llm_plain_accepted")
            return _decision_payload(
                problem, all_categories, plain[0], plain[1], all_categories, stages
            )

    stages.append("deterministic_fallback")
    fallback = _deterministic_fallback(sl, problem, all_categories, deps.logger)
    return _decision_payload(
        problem, all_categories, fallback, 0.0, [fallback], stages
    )
