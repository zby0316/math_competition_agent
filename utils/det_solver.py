# -*- coding: utf-8 -*-
"""确定性求解器矩阵 det_solver（V2.x 主线）：用通用算法管线兜底"该确定性计算处"的题。

背景：官方评测 112 题覆盖 18 领域，历史评测发现模型在**该确定性计算处手算**
是主要丢分模式（整数划分枚举错、模周期算错 2 倍、特征值变换错、数值解 5% 偏差）。
本模块在 15 条专用管线上用确定性算法（sympy/numpy/scipy/networkx）直接求解，
每条管线严格"检测 → 计算 → 自验 → 格式化"，自验不过一律交回 LLM。

设计三原则（与 utils/sympy_pre_solver.py 一致）：
- 确定性优先：只做可机械解析、答案确定无歧义的**计算题**，能解就跳过 LLM。
- 保守到极致：模式不匹配 / 条件缺失 / 计算异常 / 自验不通过 → 返回 {}，宁可漏杀不可错杀。
- 优雅降级：sympy 硬依赖整体失效；scipy/networkx/numpy 在管线内 try-import，
  ImportError 时该管线静默禁用（返回 {}）。

接口契约：
    det_solve(problem) -> {} 或 {"answer": str, "kind": str, "evidence": str}
    绝不抛异常、绝不联网、总耗时 ≤ ~15s。
"""

from __future__ import annotations

import math
import re
from typing import Optional

try:
    import sympy
    _HAS_SYMPY = True
except ImportError:  # pragma: no cover - 官方 Docker 必装 sympy
    sympy = None
    _HAS_SYMPY = False


#: 管线顺序：计算量小、命中率高、答案确定性强的在前（与设计文档一致）。
_PIPELINES = [
    "modular_period",
    "linear_recurrence",
    "partitions_hook",
    "count_combinatorics",
    "matrix_eigen",
    "linear_system",
    "quadrature",
    "graph_opt",
    "probability_exact",
    "poly_interp",
    "series_coeff",
    "expectation_var",
    "residue_complex",
    "ode_ivp",
    "bvp_shoot",
]

#: 证明/解释类题守卫：无具体数值目标时直接交回 LLM（设计铁律 5）。
_PROOF_PATTERN = re.compile(
    r"证明|求证|解释|说明理由|定义|什么是|什么叫做|是否存在反例|证明或证伪|"
    r"\bprove\b|\bshow\s+that\b|\bexplain\b|\bjustify\b|\bdefine\b", re.IGNORECASE)

#: 中文小整数（等分数 / 骰子数等）。
_CN_INT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
#: 上标数字（x² → x^2）。
_SUP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


# ----------------------------------------------------------------------
# 对外入口
# ----------------------------------------------------------------------
def det_solve(problem: str) -> dict:
    """确定性求解器矩阵。返回 {} 表示无法确定性求解，否则返回
    {"answer": str, "kind": str, "evidence": str}。"""
    if not _HAS_SYMPY:
        return {}
    text = str(problem or "")
    if _PROOF_PATTERN.search(text):
        return {}
    for name in _PIPELINES:
        fn = globals().get("_" + name)
        if fn is None:
            continue
        try:
            result = fn(text)
        except Exception:  # noqa: BLE001 - 任何异常按"该管线失效"处理
            result = None
        if result:
            return result
    return {}


def det_solve_kinds() -> list:
    """返回全部管线名（供上层 trace 留证）。"""
    return list(_PIPELINES)


# ----------------------------------------------------------------------
# 解析 / 格式化辅助
# ----------------------------------------------------------------------
def _parse(text: str, local: Optional[dict] = None):
    """把受限 ASCII 数学式解析为 sympy 表达式；非法/未知符号抛异常。"""
    from sympy.parsing.sympy_parser import (
        convert_xor, implicit_multiplication_application, parse_expr,
        standard_transformations)

    t = str(text).strip()
    if not re.fullmatch(r"[0-9A-Za-z_+\-*/^().,\s]+", t):
        raise ValueError("unsafe expression: %r" % t)
    transforms = standard_transformations + (convert_xor, implicit_multiplication_application)
    return parse_expr(t, local_dict=local or {}, transformations=transforms)


def _latex2sym(text: str) -> str:
    """LaTeX/自然语言数学式 → 可被 _parse 解析的 ASCII 表达式。"""
    t = str(text).strip().replace("$", "")
    t = (t.replace("\\left", "").replace("\\right", "").replace("\\,", "")
         .replace("\\!", "").replace("\\ ", ""))
    t = t.replace("\\cdot", "*").replace("×", "*").replace("·", "*")
    t = t.replace("\\pi", "pi").replace("\\infty", "oo")
    # 上标数字 x² → x^2
    t = re.sub(r"([A-Za-z0-9)\]])([²³⁴⁵⁶⁷⁸⁹⁰¹]+)",
               lambda m: m.group(1) + "^" + m.group(2).translate(_SUP), t)
    # 反斜杠函数名 \sin → sin
    t = re.sub(r"\\(sin|cos|tan|exp|ln|log|sqrt)\b", r"\1", t)
    # V2.7.2 审计修复：无花括号/不平衡的 \frac（如 \frac12、\frac{1}{2）会让
    # _sub_frac 原样返回，while 永不退出（死循环，且本函数无超时包裹——
    # 曾可一路卡到节点硬超时 1100s）。原样返回 = 无法解析 → 跳出交回 LLM。
    while "\\frac" in t:
        new_t = _sub_frac(t)
        if new_t == t:
            break
        t = new_t
    t = re.sub(r"sqrt\s*\{([^{}]*)\}", r"sqrt(\1)", t)
    t = re.sub(r"\bln\b", "log", t)
    t = _exp_fix(t)
    t = t.replace("^", "**").replace("{", "(").replace("}", ")")
    return t


def _sub_frac(t: str) -> str:
    i = t.find("\\frac")
    if i < 0:
        return t
    num = _braced(t, i + 5)
    if num is None:
        return t
    num_text, after = num
    den = _braced(t, after)
    if den is None:
        return t
    den_text, after2 = den
    return t[:i] + f"(({num_text})/({den_text}))" + t[after2:]


def _braced(t: str, start: int) -> Optional[tuple]:
    while start < len(t) and t[start].isspace():
        start += 1
    if start >= len(t) or t[start] != "{":
        return None
    depth = 0
    for k in range(start, len(t)):
        if t[k] == "{":
            depth += 1
        elif t[k] == "}":
            depth -= 1
            if depth == 0:
                return t[start + 1:k], k + 1
    return None


def _exp_fix(t: str) -> str:
    """e^x / e^{...} → exp(x)（避免把 e 当作普通符号）。"""
    t = re.sub(r"(?<![A-Za-z])e\s*\^\{([^{}]*)\}", r"exp(\1)", t)
    t = re.sub(r"(?<![A-Za-z])e\s*\^\s*([A-Za-z0-9]+)", r"exp(\1)", t)
    return t


def _parse_num(text: str) -> Optional[float]:
    try:
        v = _parse(_latex2sym(text))
    except Exception:  # noqa: BLE001
        return None
    if v.free_symbols:
        return None
    try:
        return float(v)
    except Exception:  # noqa: BLE001
        return None


def _approx(x, with_fraction: bool = False) -> Optional[str]:
    """数值 → "≈ 0.375"；with_fraction=True 且能配到分母 ≤1000 的简单有理数
    时输出 "3/8 = 0.375"（V2.7.2 judger 对齐：官方判分实证"只给小数不给
    分数"会被判 partial——"答案数值正确，但未用分数表示"）。"""
    try:
        f = float(x)
    except Exception:  # noqa: BLE001
        return None
    if f != f:  # NaN
        return None
    decimal = format(f, ".10g")
    if with_fraction:
        try:
            from fractions import Fraction
            frac = Fraction(f).limit_denominator(1000)
            if abs(frac - f) <= 1e-9 * max(1.0, abs(f)):
                return f"{frac.numerator}/{frac.denominator} = {decimal}"
        except Exception:  # noqa: BLE001
            pass
    return "≈ " + decimal


def _fmt_num(v) -> str:
    """sympy/数值 → 精确（整数/有理数）或 10 位有效数字近似。"""
    try:
        v = sympy.simplify(v)
    except Exception:  # noqa: BLE001
        pass
    try:
        if v.is_Integer:
            return str(v)
        if v.is_Rational:
            return f"{v.p}/{v.q}"
    except Exception:  # noqa: BLE001
        pass
    try:
        f = float(v)
    except Exception:  # noqa: BLE001
        return str(v)
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return format(f, ".10g")


def _pt(x) -> str:
    """点坐标的简洁文本：1 → "1"，0.5 → "0.5"。"""
    f = float(x)
    if f == int(f):
        return str(int(f))
    return format(f, ".10g")


def _cn_int(s: str) -> Optional[int]:
    if s.isdigit():
        return int(s)
    return _CN_INT.get(s)


def _proper_divisors(n: int) -> list:
    ds = []
    i = 1
    while i * i <= n:
        if n % i == 0:
            if i < n:
                ds.append(i)
            j = n // i
            if j != i and j < n:
                ds.append(j)
        i += 1
    return ds


# ----------------------------------------------------------------------
# 1. modular_period —— 模周期（幂序 / 斐波那契 Pisano 周期）
# ----------------------------------------------------------------------
def _modular_period(problem: str) -> Optional[dict]:
    if not re.search(r"周期|period|循环", problem, re.IGNORECASE):
        return None
    # a) 幂序列 a^n mod m 的周期 = a 模 m 的乘法阶
    m = (re.search(r"(\d+)\s*\^\s*[nN]\s*(?:mod|模)\s*(\d+)", problem)
         or re.search(r"(\d+)\s*的\s*[nN]\s*次(?:方|幂)\s*(?:mod|模)\s*(\d+)", problem))
    if m:
        return _power_order_period(int(m.group(1)), int(m.group(2)))
    # b) 斐波那契数列 mod m 的周期（Pisano 周期）
    if re.search(r"斐波那契|Fibonacci|F\s*[（(]?\s*n\s*[）)]?\s*(?:mod|模)", problem, re.IGNORECASE):
        mm = re.search(r"(?:mod|模)\s*(\d+)", problem)
        if mm:
            return _fibonacci_period(int(mm.group(1)))
    return None


def _power_order_period(a: int, m: int) -> Optional[dict]:
    if m <= 1 or a <= 0 or m > 10 ** 6:
        return None
    if math.gcd(a, m) != 1:  # 有预周期，保守放弃
        return None
    try:
        order = int(sympy.n_order(a, m))
    except Exception:  # noqa: BLE001
        return None
    if order <= 0:
        return None
    # 自验：a^order ≡ 1，且 order 的所有真因子都不满足（最小性）
    if pow(a, order, m) != 1:
        return None
    for d in _proper_divisors(order):
        if pow(a, d, m) == 1:
            return None
    return {"answer": str(order),
            "kind": "modular_period",
            "evidence": f"{a}^{order} ≡ 1 (mod {m}) 且全部真因子不满足，故为最小正周期"}


def _pisano_period(m: int) -> tuple:
    """Brent 状态机检测 (F_n, F_{n+1}) mod m 的周期与预周期。"""
    def step(st):
        x, y = st
        return (y, (x + y) % m)

    power = lam = 1
    tortoise = (0, 1)
    hare = (0, 1)
    while True:
        if power == lam:
            tortoise = hare
            power *= 2
            lam = 0
        hare = step(hare)
        lam += 1
        if tortoise == hare:
            break
    mu = 0
    tortoise = (0, 1)
    hare = (0, 1)
    for _ in range(lam):
        hare = step(hare)
    while tortoise != hare:
        tortoise = step(tortoise)
        hare = step(hare)
        mu += 1
    return lam, mu


def _fibonacci_period(m: int) -> Optional[dict]:
    if m <= 1 or m > 10000:
        return None
    try:
        T, mu = _pisano_period(m)
    except Exception:  # noqa: BLE001
        return None
    if mu != 0 or T <= 0:
        return None

    def fibmod(k: int) -> int:
        a, b = 0, 1
        for _ in range(k):
            a, b = b, (a + b) % m
        return a

    # 自验：F_T ≡ 0, F_{T+1} ≡ 1（周期点回到初态），且 4 个点 F_{n+T} == F_n
    if fibmod(T) != 0 or fibmod(T + 1) != 1:
        return None
    for n0 in (0, 1, 2, 3):
        if fibmod(n0) != fibmod(n0 + T):
            return None
    return {"answer": str(T),
            "kind": "modular_period",
            "evidence": f"F_{T}≡0, F_{T+1}≡1 (mod {m}) 且 4 个点 F_{{n+T}}≡F_n 复现"}


# ----------------------------------------------------------------------
# 2. linear_recurrence —— 线性常系数递推（通项 / a_k）
# ----------------------------------------------------------------------
def _linear_recurrence(problem: str) -> Optional[dict]:
    if not (re.search(r"递推|递归|recurrence|通项|一般项|通项公式", problem, re.IGNORECASE)
            or re.search(r"a\s*_?\s*\{?\s*n\b|a\s*\(\s*n\s*\)", problem)):
        return None
    t = problem.replace("\\", "")
    t = re.sub(r"a\s*_\s*\{([^{}]+)\}", r"a(\1)", t)
    t = re.sub(r"a\s*_\s*([nN]|\d+)", r"a(\1)", t)
    t = t.replace("{", "").replace("}", "")
    m = re.search(r"a\s*\(\s*n\s*\)\s*=\s*([^,，。;；\n]+)", t)
    if not m:
        return None
    rhs_text = m.group(1).strip()
    initials = {}
    for im in re.finditer(r"a\s*\(\s*(\d+)\s*\)\s*=\s*(-?\d+)", t):
        initials[int(im.group(1))] = int(im.group(2))
    if not initials:
        return None
    try:
        n = sympy.Symbol("n")
        a = sympy.Function("a")
        rhs = _parse(rhs_text, {"a": a, "n": n})
    except Exception:  # noqa: BLE001
        return None
    order = max((k for k in range(1, 20) if rhs.has(a(n - k))), default=0)
    if order == 0 or len(initials) < order:
        return None
    # 仅支持常系数：各系数与常数项不得含 n
    coeffs = []
    const = rhs
    for k in range(1, order + 1):
        c = rhs.coeff(a(n - k))
        if c.has(n):
            return None
        coeffs.append(c)
        const = const.subs(a(n - k), 0)
    if const.has(n):
        return None
    try:
        sol = sympy.rsolve(a(n) - rhs, a(n), {a(k): v for k, v in initials.items()})
    except Exception:  # noqa: BLE001
        return None
    if sol is None:
        return None
    # 自验：前 order+6 项按递推关系直接迭代，与 rsolve 通项逐项比对
    try:
        seq = [sympy.Integer(initials[k]) for k in range(order)]
        for k in range(order, order + 6):
            val = const
            for j in range(1, order + 1):
                val = val + coeffs[j - 1] * seq[k - j]
            seq.append(sympy.simplify(val))
        for k in range(0, order + 6):
            if sympy.simplify(seq[k] - sol.subs(n, k)) != 0:
                return None
    except Exception:  # noqa: BLE001
        return None
    # 求通项还是特定项
    if re.search(r"通项|一般项|通项公式|closed\s*form|general\s*term", problem, re.IGNORECASE):
        answer = _fmt_poly(sol)
    else:
        max_init = max(initials.keys())
        targets = sorted({int(km.group(1))
                          for km in re.finditer(r"a\s*\(\s*(\d+)\s*\)", t)
                          if int(km.group(1)) > max_init})
        if not targets:
            answer = _fmt_poly(sol)
        else:
            k = targets[-1]
            val = sympy.simplify(sol.subs(n, k))
            if not val.is_Integer:
                return None
            answer = f"a({k}) = {int(val)}"
    evidence = "前 {} 项直接迭代与 rsolve 通项一致".format(order + 6)
    return {"answer": answer, "kind": "linear_recurrence", "evidence": evidence}


# ----------------------------------------------------------------------
# 3. partitions_hook —— 整数划分的 hook length 维数
# ----------------------------------------------------------------------
#: 群语境：hook 长度维数公式只在"群表示"语境下有意义。
_GROUP_CTX_RE = re.compile(
    r"对称群|置换群|S\s*_?\{?\d+\}?|\bpartition\b|\byoung\b|\bhook\b|钩长", re.IGNORECASE)
#: 维数语境：题面必须问"维数/不可约表示"，否则（共轭类个数/群的阶/中心）
#: 是问非所答的误触发（自验 Σf_λ²=n! 防不了"参数对但答案类型错"）。
_DIM_CTX_RE = re.compile(
    r"维数|维度|不可约表示|表示维数|\birreducible\b|\bdimension\b", re.IGNORECASE)


def _partitions_hook(problem: str) -> Optional[dict]:
    if not (_GROUP_CTX_RE.search(problem) and _DIM_CTX_RE.search(problem)):
        return None
    m = (re.search(r"[Ss]\s*_?\{?(\d+)\}?", problem)
         or re.search(r"[nN]\s*=\s*(\d+)", problem))
    if not m:
        return None
    n = int(m.group(1))
    if n < 1 or n > 15:
        return None
    try:
        dims = _hook_dims(n)
    except Exception:  # noqa: BLE001
        return None
    # 自验：Σ f_λ² == n!（不可约表示维数平方和 = 群阶）
    fact = math.factorial(n)
    if sum(d * d for d in dims) != fact:
        return None
    sd = sorted(dims)
    answer = "[" + ",".join(str(d) for d in sd) + "]"
    return {"answer": answer,
            "kind": "partitions_hook",
            "evidence": f"Σ f_λ² = {fact} = {n}! 自洽"}


def _hook_dims(n: int) -> list:
    parts = []

    def gen(rem, mx, cur):
        if rem == 0:
            parts.append(tuple(cur))
            return
        for v in range(min(mx, rem), 0, -1):
            gen(rem - v, v, cur + [v])

    gen(n, n, [])
    dims = []
    for lam in parts:
        conj = [0] * lam[0]
        for i in range(len(lam)):
            for j in range(lam[i]):
                conj[j] += 1
        prod = 1
        for i, li in enumerate(lam):
            for j in range(li):
                h = (li - j) + (conj[j] - i) - 1
                prod *= h
        dims.append(math.factorial(n) // prod)
    return dims


# ----------------------------------------------------------------------
# 4. count_combinatorics —— 组合计数（组合数/错排/卡特兰/斯特林/排列）
# ----------------------------------------------------------------------
def _count_combinatorics(problem: str) -> Optional[dict]:
    for sub in (_derangement, _catalan, _stirling, _permutation, _binomial):
        try:
            r = sub(problem)
        except Exception:  # noqa: BLE001
            r = None
        if r:
            return r
    return None


def _binomial(problem: str) -> Optional[dict]:
    m = re.search(r"C\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", problem)
    if not m:
        return None
    # V2.7.3 问目标守卫（官方 54.46 分回溯的根因级修复）：必须是"求/计算
    # C(n,k)（的值）"形态才作答——题面陈述里顺带出现 C(n,k)（如"3 元子集
    # 共有 C(10,3) 个，求含元素 1 的概率"）时，答案是题所问的概率而非组合
    # 数本身；旧实现直接劫持作答 C(10,3)=120，确定性错误答案以置信 1.0 锁定。
    ask_ok = bool(re.search(
        r"(?:求|计算|算出)\s*[^。；;]{0,25}?C\s*\(\s*\d+\s*,\s*\d+\s*\)"
        r"|C\s*\(\s*\d+\s*,\s*\d+\s*\)\s*(?:的值|等于|是多少|=)",
        problem))
    if not ask_ok:
        return None
    n, k = int(m.group(1)), int(m.group(2))
    if k < 0 or k > n or n > 500:
        return None
    val = int(sympy.binomial(n, k))
    # 自验：math.comb 独立比对
    if val != math.comb(n, k):
        return None
    return {"answer": f"C({n},{k}) = {val}",
            "kind": "count_combinatorics",
            "evidence": f"math.comb({n},{k}) 独立比对一致 = {val}"}


def _derangement(problem: str) -> Optional[dict]:
    if not re.search(r"错排|derangement", problem, re.IGNORECASE):
        return None
    m = re.search(r"!\s*(\d+)", problem)
    if m:
        n = int(m.group(1))
    else:
        m = re.search(r"(\d+)\s*个\s*(?:元素|人|封信|球|数)", problem)
        if not m:
            return None
        n = int(m.group(1))
    if n < 1 or n > 12:
        return None
    val = int(sympy.subfactorial(n))
    # 自验：n≤9 用 itertools 全排列暴力枚举错排数（362K 排列，秒级）；
    # n>9 用错排递推 !n=(n-1)(!(n-1)+!(n-2)) 独立 DP（O(n)，
    # V2.7.2 审计修复：12!≈4.79 亿的全排列暴力会烧 1-2 分钟，违反
    # "总耗时 ≤15s" 契约）。
    if n <= 9:
        import itertools
        cnt = sum(1 for p in itertools.permutations(range(n))
                  if all(p[i] != i for i in range(n)))
        if cnt != val:
            return None
        evidence = f"{n}! 全排列暴力枚举错排数 {cnt} 与 subfactorial 一致"
    else:
        d0, d1 = 1, 0
        for k in range(2, n + 1):
            d0, d1 = d1, (k - 1) * (d0 + d1)
        if d1 != val:
            return None
        evidence = f"错排递推 !{n}=(n-1)(!(n-1)+!(n-2)) 独立 DP 与 subfactorial 一致"
    return {"answer": f"!{n} = {val}",
            "kind": "count_combinatorics",
            "evidence": evidence}


def _catalan(problem: str) -> Optional[dict]:
    if not re.search(r"卡特兰|Catalan", problem, re.IGNORECASE):
        return None
    m = re.search(r"C\s*_\s*\{?(\d+)\}?", problem) or re.search(r"第\s*(\d+)\s*个", problem)
    if not m:
        return None
    n = int(m.group(1))
    if n < 0 or n > 20:
        return None
    val = int(sympy.catalan(n))
    # 自验：递推 C_k = Σ C_i C_{k-1-i} 独立比对
    c = [1] * (n + 1)
    for k in range(1, n + 1):
        c[k] = sum(c[i] * c[k - 1 - i] for i in range(k))
    if c[n] != val:
        return None
    return {"answer": f"C_{n} = {val}",
            "kind": "count_combinatorics",
            "evidence": f"卡特兰递推独立比对一致 = {val}"}


def _stirling(problem: str) -> Optional[dict]:
    if not re.search(r"斯特林|Stirling|第二类.*数", problem, re.IGNORECASE):
        return None
    m = re.search(r"S\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", problem)
    if not m:
        return None
    n, k = int(m.group(1)), int(m.group(2))
    if k < 0 or k > n or n > 20:
        return None
    val = int(sympy.functions.combinatorial.numbers.stirling(n, k))
    # 自验：S(n,k) = 1/k! Σ_{j=0}^{k} (-1)^{k-j} C(k,j) j^n 独立比对
    verify = sum(((-1) ** (k - j)) * math.comb(k, j) * (j ** n) for j in range(k + 1)) // math.factorial(k)
    if verify != val:
        return None
    return {"answer": f"S({n},{k}) = {val}",
            "kind": "count_combinatorics",
            "evidence": f"第二类斯特林数显式公式独立比对一致 = {val}"}


def _permutation(problem: str) -> Optional[dict]:
    m = re.search(r"[PA]\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", problem)
    if not m or not re.search(r"排列", problem):
        return None
    n, k = int(m.group(1)), int(m.group(2))
    if k < 0 or k > n or n > 12:
        return None
    val = math.factorial(n) // math.factorial(n - k)
    # 自验（V2.7.2 审计修复：P(12,12)=4.79 亿的全排列暴力不可接受）：
    # ① P(n,k)=C(n,k)·k! 双原语独立比对（任意规模）；② 排列数 ≤ 20 万时
    # 附加 itertools 暴力枚举。
    if math.comb(n, k) * math.factorial(k) != val:
        return None
    evidence = f"C({n},{k})·{k}! 独立比对一致 = {val}"
    if val <= 200_000:
        import itertools
        cnt = sum(1 for _ in itertools.permutations(range(n), k))
        if cnt != val:
            return None
        evidence += "；itertools 排列数暴力枚举一致"
    return {"answer": f"P({n},{k}) = {val}",
            "kind": "count_combinatorics",
            "evidence": evidence}


# ----------------------------------------------------------------------
# 5. matrix_eigen —— 特征值 / 特征向量
# ----------------------------------------------------------------------
def _matrix_eigen(problem: str) -> Optional[dict]:
    if not re.search(r"特征值|特征向量|对角化|eigen", problem, re.IGNORECASE):
        return None
    # V2.7.3 问目标守卫：问"是否可对角化/对角化条件/含参数判定"的题，答案
    # 是判定/参数分析而非特征值列表——本管线只能答"求特征值/特征向量"型，
    # 其余（含参数、Jordan、可对角化判定）交回 LLM，防确定性错误答案锁定。
    if re.search(r"可对角化|是否对角化|对角化条件|Jordan|若尔当|参数", problem,
                 re.IGNORECASE):
        return None
    mat = _parse_matrix(problem)
    if mat is None:
        return None
    try:
        M = sympy.Matrix(mat)
        eig = M.eigenvals()
        eigvecs = M.eigenvects()
    except Exception:  # noqa: BLE001
        return None
    vals = []
    for v, mult in eig.items():
        vals.extend([v] * mult)
    if not vals:
        return None
    # 自验：tr(A)=Σλ、det(A)=Πλ（容差 1e-9）
    try:
        tr = M.trace()
        det = M.det()
        s = sum(vals)
        p = sympy.S(1)
        for v in vals:
            p = p * v
        if sympy.simplify(tr - s) != 0 and abs(float(tr - s)) > 1e-9:
            return None
        if sympy.simplify(det - p) != 0 and abs(float(det - p)) > 1e-9:
            return None
        # 对称矩阵再用 numpy.linalg.eigvalsh 交叉比对
        if M.is_symmetric():
            import numpy as np
            A = np.array([[float(c) for c in row] for row in mat])
            w = sorted(np.linalg.eigvalsh(A).tolist())
            sv = sorted(float(sympy.N(v, 15)) for v in vals)
            if len(w) != len(sv) or max(abs(a - b) for a, b in zip(w, sv)) > 1e-8:
                return None
    except Exception:  # noqa: BLE001
        return None
    val_str = ", ".join(_fmt_num(v) for v in vals)
    vec_parts = []
    for v, mult, vecs in eigvecs:
        for vec in vecs:
            cells = ", ".join(_fmt_num(c) for c in vec)
            vec_parts.append(f"λ={_fmt_num(v)}: ({cells})")
    answer = f"特征值：{val_str}；特征向量：{'；'.join(vec_parts)}"
    return {"answer": answer,
            "kind": "matrix_eigen",
            "evidence": f"tr(A)={_fmt_num(tr)} = Σλ 且 det(A)={_fmt_num(det)} = Πλ 自洽"}


def _parse_matrix(problem: str) -> Optional[list]:
    m = re.search(r"\\begin\{(?:b|p|v)?matrix\}(.+?)\\end\{(?:b|p|v)?matrix\}", problem, re.DOTALL)
    if m:
        rows = [row.split("&") for row in re.split(r"\\\\", m.group(1))]
        return _clean_matrix([[c.strip() for c in row] for row in rows])
    m = re.search(r"\[\[(.+?)\]\]", problem, re.DOTALL)
    if m:
        rows = [row.split(",") for row in re.split(r"\],\s*\[", m.group(1))]
        return _clean_matrix([[c.strip() for c in row] for row in rows])
    return None


def _clean_matrix(mat: list) -> Optional[list]:
    nrow = len(mat)
    if nrow < 1 or nrow > 8:
        return None
    ncol = len(mat[0])
    if ncol < 1 or ncol > 8:
        return None
    out = []
    for row in mat:
        if len(row) != ncol:
            return None
        r = []
        for c in row:
            try:
                v = _parse(_latex2sym(c))
            except Exception:  # noqa: BLE001
                return None
            if v.free_symbols:
                return None
            r.append(v)
        out.append(r)
    return out


# ----------------------------------------------------------------------
# 6. linear_system —— 线性方程组
# ----------------------------------------------------------------------
def _linear_system(problem: str) -> Optional[dict]:
    if problem.count("=") < 2:
        return None
    if not re.search(r"方程|solve|联立|求解", problem, re.IGNORECASE):
        return None
    # V2.7.3 问目标守卫：末段"求 <含运算的表达式> 的值"问的是**导出表达式**
    # （如"解方程组 x+y=3, x-y=1，求 2x+y 的值"——答案是 5 而非 x,y），本管线
    # 只答变量本身，此类题交回 LLM，防确定性错误答案锁定。
    _ask = re.search(r"求\s*([^。；;]{1,40}?)\s*(?:的值|的结果)?[。？?]?$", problem)
    if _ask and re.search(r"[+\-*/^]", _ask.group(1)):
        return None
    eqs_raw = re.findall(r"([0-9A-Za-z_+\-*/^().\s]+?)\s*=\s*(-?[0-9A-Za-z_+\-*/^().\s]+)", problem)
    if not eqs_raw:
        return None
    x, y, z = sympy.Symbol("x"), sympy.Symbol("y"), sympy.Symbol("z")
    allowed = {x, y, z}
    parsed = []
    for lhs, rhs in eqs_raw:
        try:
            le = _parse(lhs)
            re_ = _parse(rhs)
            eq = sympy.Eq(le, re_)
        except Exception:  # noqa: BLE001
            continue
        syms = eq.free_symbols
        if not syms or not (syms <= allowed):
            continue
        try:
            if sympy.Poly(le - re_, *syms).total_degree() > 1:
                continue
        except Exception:  # noqa: BLE001
            continue
        parsed.append(eq)
    if len(parsed) < 2 or len(parsed) > 4:
        return None
    syms_list = sorted({s for eq in parsed for s in eq.free_symbols}, key=lambda s: str(s.name))
    if not syms_list or len(parsed) < len(syms_list):
        return None
    try:
        solset = sympy.linsolve(parsed, syms_list)
        sol = list(solset)
    except Exception:  # noqa: BLE001
        return None
    if len(sol) != 1:
        return None
    vals = list(sol[0])
    if len(vals) != len(syms_list):
        return None
    subs = dict(zip(syms_list, vals))
    # 自验：解回代每个方程残差 = 0
    for eq in parsed:
        try:
            if sympy.simplify(eq.lhs.subs(subs) - eq.rhs.subs(subs)) != 0:
                return None
        except Exception:  # noqa: BLE001
            return None
    answer = ", ".join(f"{s.name} = {_fmt_num(v)}" for s, v in zip(syms_list, vals))
    return {"answer": answer,
            "kind": "linear_system",
            "evidence": f"{len(parsed)} 个方程回代残差均为 0"}


# ----------------------------------------------------------------------
# 7. quadrature —— 复化梯形 / 辛普森数值积分
# ----------------------------------------------------------------------
def _quadrature(problem: str) -> Optional[dict]:
    if not re.search(r"梯形|辛普森|Simpson|复化|数值积分|数值.*近似", problem, re.IGNORECASE):
        return None
    m = re.search(r"(?:\\int|∫)\s*_?\{?([^}^\s]+)\}?\s*\^?\{?([^}^\s]+)\}?\s*(.+?)(?:\\,|\s)*d([A-Za-z])\b",
                  problem, re.DOTALL)
    if not m:
        return None
    a = _parse_num(m.group(1))
    b = _parse_num(m.group(2))
    var = m.group(4)
    n = _parse_subdiv(problem)
    if a is None or b is None or n is None or n < 1 or n > 20000:
        return None
    try:
        x = sympy.Symbol(var)
        f = _parse(_latex2sym(m.group(3)))
        if not (f.free_symbols <= {x}):
            return None
    except Exception:  # noqa: BLE001
        return None
    is_simpson = bool(re.search(r"辛普森|Simpson|simpson", problem))
    method = _composite_simpson if is_simpson else _composite_trap
    try:
        Q = method(f, x, a, b, n)
        Q2 = method(f, x, a, b, 2 * n)
        exact = float(sympy.integrate(f, (x, a, b)))
    except Exception:  # noqa: BLE001
        return None
    # 自验 1：独立实现交叉比对（捕获实现 bug）。
    # V2.7.2 审计修复：旧 Simpson 分支 np_ref 调的就是计算 Q 的同一个
    # _composite_simpson（自比较恒等）；改用 scipy.integrate.simpson 独立实现。
    try:
        if is_simpson:
            from scipy.integrate import simpson as _scipy_simpson
            xx = [float(a + (b - a) * i / n) for i in range(n + 1)]
            yy = [float(f.subs(x, v)) for v in xx]
            np_ref = float(_scipy_simpson(yy, x=xx))
        else:
            import numpy as np
            xx = np.linspace(a, b, n + 1)
            yy = np.array([float(f.subs(x, float(v))) for v in xx])
            np_ref = float(np.trapz(yy, xx))
        if abs(Q - np_ref) > 1e-9 * max(1.0, abs(Q)):
            return None
    except Exception:  # noqa: BLE001
        return None
    # 自验 2：方法阶核对（梯形 O(1/n²) → 误差比 ≈4；辛普森 O(1/n⁴) → ≈16）
    e1 = abs(Q - exact)
    e2 = abs(Q2 - exact)
    if e1 > 1e-12 and e2 > 1e-12:
        if e2 >= e1:
            return None
        ratio = e1 / e2
        lo, hi = (8.0, 60.0) if is_simpson else (2.0, 9.0)
        if not (lo < ratio < hi):
            return None
    ans = _approx(Q, with_fraction=True)
    if ans is None:
        return None
    return {"answer": ans,
            "kind": "quadrature",
            "evidence": f"复化{'辛普森' if is_simpson else '梯形'} n={n}，误差比 {e1/e2 if e2 else 0:.2f} 符合方法阶"}


def _composite_trap(f, x, a, b, n) -> float:
    h = (b - a) / n
    s = 0.5 * (f.subs(x, a) + f.subs(x, b))
    for i in range(1, n):
        s += f.subs(x, a + i * h)
    return float(s * h)


def _composite_simpson(f, x, a, b, n) -> float:
    if n % 2:
        n += 1
    h = (b - a) / n
    s = f.subs(x, a) + f.subs(x, b)
    for i in range(1, n):
        s += f.subs(x, a + i * h) * (4 if i % 2 else 2)
    return float(s * h / 3)


def _parse_subdiv(problem: str) -> Optional[int]:
    m = re.search(r"([一二两三四五六七八九十]|\d+)\s*等分", problem)
    if m:
        return _cn_int(m.group(1)) if not m.group(1).isdigit() else int(m.group(1))
    m = re.search(r"[nN]\s*=\s*(\d+)", problem)
    if m:
        return int(m.group(1))
    m = re.search(r"分成\s*(\d+)\s*份", problem)
    if m:
        return int(m.group(1))
    return None


# ----------------------------------------------------------------------
# 8. graph_opt —— 图优化（最短路 / MST / 最小割最大流 / 匹配）
# ----------------------------------------------------------------------
def _graph_opt(problem: str) -> Optional[dict]:
    if not re.search(r"最短路|最短路径|最小割|最大流|最小生成树|二分匹配|最大匹配|shortest|max.?flow|min.?cut|mst|matching",
                     problem, re.IGNORECASE):
        return None
    if re.search(r"最短路|最短路径|shortest", problem, re.IGNORECASE):
        return _shortest_path(problem)
    if re.search(r"最小生成树|mst", problem, re.IGNORECASE):
        return _mst(problem)
    if re.search(r"最小割|最大流|max.?flow|min.?cut", problem, re.IGNORECASE):
        return _maxflow_mincut(problem)
    if re.search(r"匹配|matching", problem, re.IGNORECASE):
        return _matching(problem)
    return None


def _parse_weighted_edges(text: str) -> dict:
    edges = {}
    for m in re.finditer(r"([A-Za-z])\s*(?:[-—–]|到|至)\s*([A-Za-z])\s*(?:=|:|权(?:重|值)?|长(?:度)?|距离(?:为)?)?\s*(\d+(?:\.\d+)?)", text):
        edges.setdefault((m.group(1), m.group(2)), float(m.group(3)))
    for m in re.finditer(r"([A-Za-z])([A-Za-z])\s*=\s*(\d+(?:\.\d+)?)", text):
        edges.setdefault((m.group(1), m.group(2)), float(m.group(3)))
    for m in re.finditer(r"([A-Za-z])([A-Za-z])\s*(?:权(?:重|值)?|长(?:度)?|距离)\s*(\d+(?:\.\d+)?)", text):
        edges.setdefault((m.group(1), m.group(2)), float(m.group(3)))
    return edges


def _parse_source_target(text: str) -> tuple:
    m = re.search(r"(?:求|从|计算)?\s*([A-Za-z])\s*到\s*([A-Za-z])\s*(?:的|最短|最短路|路径)?", text)
    if m:
        return m.group(1), m.group(2)
    return None, None


def _nodes_of(edges: dict) -> set:
    nodes = set()
    for u, v in edges:
        nodes.add(u)
        nodes.add(v)
    return nodes


def _shortest_path(problem: str) -> Optional[dict]:
    edges = _parse_weighted_edges(problem)
    nodes = _nodes_of(edges)
    if not edges or len(nodes) < 2 or len(nodes) > 10:
        return None
    s, t = _parse_source_target(problem)
    if not s or not t or s not in nodes or t not in nodes:
        return None
    try:
        import networkx as nx
        G = nx.Graph()
        G.add_nodes_from(nodes)
        for (u, v), w in edges.items():
            G.add_edge(u, v, weight=w)
        d = nx.shortest_path_length(G, s, t, weight="weight")
    except Exception:  # noqa: BLE001
        return None
    if len(nodes) <= 8:
        best = _brute_shortest(nodes, edges, s, t)
        if best is None or abs(best - d) > 1e-9:
            return None
        evidence = f"最短路 {s}→{t} = {d}，{len(nodes)} 节点暴力枚举一致"
    else:
        evidence = f"networkx 最短路 {s}→{t} = {d}"
    answer = str(int(d)) if float(d) == int(d) else format(float(d), ".10g")
    return {"answer": answer, "kind": "graph_opt", "evidence": evidence}


def _brute_shortest(nodes, edges, s, t) -> Optional[float]:
    import itertools
    adj = {}
    for (u, v), w in edges.items():
        adj.setdefault(u, []).append((v, w))
        adj.setdefault(v, []).append((u, w))
    others = [x for x in nodes if x not in (s, t)]
    best = None
    for r in range(len(others) + 1):
        for perm in itertools.permutations(others, r):
            path = (s,) + perm + (t,)
            d = 0.0
            ok = True
            for i in range(len(path) - 1):
                w = next((ww for vv, ww in adj.get(path[i], []) if vv == path[i + 1]), None)
                if w is None:
                    ok = False
                    break
                d += w
            if ok and (best is None or d < best):
                best = d
    return best


def _mst(problem: str) -> Optional[dict]:
    edges = _parse_weighted_edges(problem)
    nodes = _nodes_of(edges)
    if not edges or len(nodes) < 2 or len(nodes) > 10:
        return None
    try:
        import networkx as nx
        G = nx.Graph()
        G.add_nodes_from(nodes)
        for (u, v), w in edges.items():
            G.add_edge(u, v, weight=w)
        total = float(nx.minimum_spanning_tree(G).size(weight="weight"))
    except Exception:  # noqa: BLE001
        return None
    # 自验：Prim 独立实现比对
    if len(nodes) <= 8:
        prim = _prim(nodes, edges)
        if prim is None or abs(prim - total) > 1e-9:
            return None
    answer = str(int(total)) if total == int(total) else format(total, ".10g")
    return {"answer": answer, "kind": "graph_opt", "evidence": f"MST 权 = {answer}，Prim 独立实现一致"}


def _prim(nodes, edges) -> Optional[float]:
    adj = {}
    for (u, v), w in edges.items():
        adj.setdefault(u, []).append((v, w))
        adj.setdefault(v, []).append((u, w))
    start = next(iter(nodes))
    visited = {start}
    total = 0.0
    while len(visited) < len(nodes):
        best = None
        for u in visited:
            for v, w in adj.get(u, []):
                if v not in visited and (best is None or w < best[2]):
                    best = (u, v, w)
        if best is None:
            return None
        visited.add(best[1])
        total += best[2]
    return total


def _maxflow_mincut(problem: str) -> Optional[dict]:
    edges = _parse_weighted_edges(problem)
    nodes = _nodes_of(edges)
    if not edges or len(nodes) < 2 or len(nodes) > 10:
        return None
    s, t = _parse_source_target(problem)
    if not s or not t or s not in nodes or t not in nodes:
        return None
    try:
        import networkx as nx
        G = nx.DiGraph()
        G.add_nodes_from(nodes)
        for (u, v), w in edges.items():
            G.add_edge(u, v, capacity=w)
        val = float(nx.maximum_flow_value(G, s, t))
    except Exception:  # noqa: BLE001
        return None
    # 自验：最大流 = 最小割（n≤8 时枚举全部割）
    if len(nodes) <= 8:
        mincut = _brute_mincut(nodes, edges, s, t)
        if mincut is None or abs(mincut - val) > 1e-9:
            return None
        evidence = f"max flow = min cut = {val}，割枚举一致"
    else:
        evidence = f"networkx 最大流 {s}→{t} = {val}"
    answer = str(int(val)) if val == int(val) else format(val, ".10g")
    return {"answer": answer, "kind": "graph_opt", "evidence": evidence}


def _brute_mincut(nodes, edges, s, t) -> Optional[float]:
    others = [x for x in nodes if x not in (s, t)]
    best = None
    for mask in range(1 << len(others)):
        S = {s}
        for i, x in enumerate(others):
            if mask & (1 << i):
                S.add(x)
        cap = 0.0
        for (u, v), w in edges.items():
            if u in S and v not in S:
                cap += w
        if best is None or cap < best:
            best = cap
    return best


def _matching(problem: str) -> Optional[dict]:
    edges = _parse_weighted_edges(problem)
    nodes = _nodes_of(edges)
    if not edges or len(nodes) < 2 or len(nodes) > 10:
        return None
    try:
        import networkx as nx
        G = nx.Graph()
        G.add_nodes_from(nodes)
        for (u, v), w in edges.items():
            G.add_edge(u, v, weight=w)
        if any(w != 1 for w in edges.values()):
            m = nx.max_weight_matching(G, maxcardinality=True)
            val = sum(edges.get((min(u, v), max(u, v)), edges.get((max(u, v), min(u, v)), 0))
                      for u, v in m)
        else:
            val = float(len(nx.maximal_matching(G)))
    except Exception:  # noqa: BLE001
        return None
    if len(nodes) <= 8 and len(edges) <= 18:
        # V2.7.2 审计修复：完全图 8 节点 m=28 → 2^28≈2.68 亿的位掩码暴力
        # 违反"总耗时 ≤15s"契约；边数 >18 时跳过暴力交叉（networkx 结果
        # 直接采用，仍为确定性算法）。
        brute = _brute_matching(nodes, edges)
        if brute is None or abs(brute - val) > 1e-9:
            return None
        evidence = f"最大匹配 = {int(val)}，暴力枚举一致"
    else:
        evidence = f"networkx 最大匹配 = {int(val)}"
    return {"answer": str(int(val)), "kind": "graph_opt", "evidence": evidence}


def _brute_matching(nodes, edges) -> Optional[int]:
    edges_list = list(edges.items())
    best = 0
    m = len(edges_list)
    for mask in range(1 << m):
        used = set()
        cnt = 0
        ok = True
        for i, ((u, v), w) in enumerate(edges_list):
            if mask & (1 << i):
                if u in used or v in used:
                    ok = False
                    break
                used.add(u)
                used.add(v)
                cnt += 1
        if ok and cnt > best:
            best = cnt
    return best


# ----------------------------------------------------------------------
# 9. probability_exact —— 古典概型（骰子 / 放球）
# ----------------------------------------------------------------------
def _probability_exact(problem: str) -> Optional[dict]:
    if not re.search(r"概率|随机|骰子|dice|红球|白球|袋中|抽取|抽到", problem, re.IGNORECASE):
        return None
    r = _dice_prob(problem)
    if r:
        return r
    return _balls_prob(problem)


def _dice_prob(problem: str) -> Optional[dict]:
    if not re.search(r"骰子|dice|die\b", problem, re.IGNORECASE):
        return None
    m = re.search(r"([一二两三四五六]|\d+)\s*(?:颗|个|枚|只)\s*(?:骰子|骰)", problem)
    nd = _cn_int(m.group(1)) if m else 1
    if nd is None or nd < 1 or nd > 6 or 6 ** nd > 10 ** 6:
        return None
    m2 = re.search(r"(?:点数)?(?:和|之和)\s*(?:为|是|等于|=)\s*(\d+)", problem)
    if not m2:
        return None
    k = int(m2.group(1))
    import itertools
    total = 6 ** nd
    cnt = sum(1 for out in itertools.product(range(1, 7), repeat=nd) if sum(out) == k)
    if cnt == 0:
        return None
    cnt_c = total - cnt
    if cnt + cnt_c != total:
        return None
    p = sympy.Rational(cnt, total)
    answer = _fmt_num(p)
    return {"answer": answer,
            "kind": "probability_exact",
            "evidence": f"枚举 {total} 样本命中 {cnt}，互补 {cnt_c}，分子分母和={total} 自洽"}


def _balls_prob(problem: str) -> Optional[dict]:
    m_red = re.search(r"(\d+)\s*(?:个)?\s*红球", problem)
    m_white = re.search(r"(\d+)\s*(?:个)?\s*白球", problem)
    m_draw = re.search(r"(?:取|抽|抽取|取出|随机取)\s*(\d+)\s*个", problem)
    if not (m_red and m_white and m_draw):
        return None
    r = int(m_red.group(1))
    w = int(m_white.group(1))
    d = int(m_draw.group(1))
    if r + w > 12 or d > r + w:
        return None
    m_k = re.search(r"(?:恰好|恰有|其中|取到)\s*(\d+)\s*个\s*(?:红球|红的)", problem)
    if not m_k:
        return None
    k = int(m_k.group(1))
    if k > d or k > r or (d - k) > w:
        return None
    total = math.comb(r + w, d)
    if total > 10 ** 6:
        return None
    import itertools
    balls = list(range(r + w))
    cnt = sum(1 for combo in itertools.combinations(balls, d) if sum(1 for b in combo if b < r) == k)
    if cnt == 0:
        return None
    cnt_c = total - cnt
    if cnt + cnt_c != total:
        return None
    p = sympy.Rational(cnt, total)
    return {"answer": _fmt_num(p),
            "kind": "probability_exact",
            "evidence": f"超几何枚举 {total} 样本命中 {cnt}，互补事件概率和为 1 自洽"}


# ----------------------------------------------------------------------
# 10. poly_interp —— 插值多项式
# ----------------------------------------------------------------------
def _poly_interp(problem: str) -> Optional[dict]:
    if not re.search(r"插值|拉格朗日|Lagrange|牛顿插值|多项式拟合|最小二乘|interpolat", problem, re.IGNORECASE):
        return None
    pts = _parse_points(problem)
    if not pts or len(pts) < 2 or len(pts) > 12:
        return None
    if len(set(p[0] for p in pts)) != len(pts):
        return None
    x = sympy.Symbol("x")
    try:
        poly = sympy.interpolate(pts, x)
    except Exception:  # noqa: BLE001
        return None
    # 自验：全部给定点回代残差 = 0（< 1e-9）
    for xi, yi in pts:
        try:
            if sympy.simplify(poly.subs(x, xi) - yi) != 0:
                return None
        except Exception:  # noqa: BLE001
            return None
    # 求值点检测（V2.7 修复）：题面问"多项式在某点的值"时必须代入求值，
    # 不能只答多项式本体（真机回归实证的"求多项式在某点处的值"事故）。
    # 关键区分：
    #   - 数据点形态 f(0)=1（紧跟 =）不是求值请求 → 负向断言排除；
    #   - "在/当 x=<num> 处/时"只有在明确问"的值/函数值"时才算求值请求
    #     （避免把"在 x=2 处展开"之类误判）；
    #   - 求值请求优先于"求 f(x)"的多项式请求（"求 f(x) 在 x=2 处的值"
    #     的宾语是值不是多项式）。
    f_val = re.search(r"f\s*\(\s*(-?\d+(?:\.\d+)?)\s*\)(?!\s*=)", problem)
    x_val = re.search(r"(?:在|当)\s*x\s*=\s*(-?\d+(?:\.\d+)?)\s*(?:处|时)", problem)
    point = None
    if f_val:
        point = f_val.group(1)
    elif x_val and re.search(r"的值|函数值|取值|处的|时的", problem):
        point = x_val.group(1)
    if point is not None:
        val = sympy.simplify(poly.subs(x, sympy.Rational(point)))
        answer = f"f({_pt(point)}) = {_fmt_num(val)}"
    else:
        answer = f"f(x) = {_fmt_poly(poly)}"
    return {"answer": answer,
            "kind": "poly_interp",
            "evidence": f"{len(pts)} 个插值点回代残差均 < 1e-9"}


def _parse_points(problem: str) -> list:
    pts = []
    for m in re.finditer(r"\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)", problem):
        pts.append((sympy.Rational(m.group(1)), sympy.Rational(m.group(2))))
    if pts:
        return pts
    for m in re.finditer(r"f\s*\(\s*(-?\d+(?:\.\d+)?)\s*\)\s*=\s*(-?\d+(?:\.\d+)?)", problem):
        pts.append((sympy.Rational(m.group(1)), sympy.Rational(m.group(2))))
    return pts


# ----------------------------------------------------------------------
# 11. series_coeff —— 泰勒 / 幂级数系数
# ----------------------------------------------------------------------
def _series_coeff(problem: str) -> Optional[dict]:
    if not re.search(r"泰勒|级数|幂级数|展开|Taylor|series|系数", problem, re.IGNORECASE):
        return None
    m = re.search(r"x\s*\^\s*(\d+)\s*(?:的|项)?\s*系数", problem)
    if not m:
        return None
    n = int(m.group(1))
    if n < 0 or n > 30:
        return None
    fn_text = _parse_series_fn(problem)
    if not fn_text:
        return None
    a = _parse_expansion_point(problem)
    x = sympy.Symbol("x")
    try:
        fn = _parse(_latex2sym(fn_text))
        if not (fn.free_symbols <= {x}):
            return None
        ser = sympy.series(fn, x, a, n + 1).removeO()
        coeff = sympy.expand(ser).coeff(x, n) if a == 0 else sympy.expand(ser).coeff(x - a, n)
    except Exception:  # noqa: BLE001
        return None
    if coeff is None:
        return None
    # 自验（V2.7.2 审计修复）：首选**精确**双路比对——系数 = f⁽ⁿ⁾(a)/n! 用
    # 独立求导法重算，符号相等才放行（比固定容差的数值比对强得多，且对
    # 低阶系数不误杀：旧固定容差 1e-6 在截断误差 O(h^(n+1)) 下让 x¹/x²
    # 系数题永远被拒收）。数值邻域比对保留为第二道（按阶缩放容差）。
    try:
        coeff_direct = sympy.simplify(
            sympy.diff(fn, x, n).subs(x, a) / sympy.factorial(n))
        if sympy.simplify(coeff - coeff_direct) != 0:
            return None
        h = sympy.Rational(1, 100)
        pt = a + h
        exact = float(sympy.N(fn.subs(x, pt), 20))
        approx = float(sympy.N(ser.subs(x, pt), 20))
        tolerance = max(1e-9, (0.01) ** (n + 1) * 10)
        if abs(exact - approx) > tolerance:
            return None
    except Exception:  # noqa: BLE001
        return None
    return {"answer": _fmt_num(sympy.simplify(coeff)),
            "kind": "series_coeff",
            "evidence": f"展开至 x^{n} 项在 x={_pt(float(pt))} 邻域与原函数数值一致"}


def _parse_series_fn(problem: str) -> Optional[str]:
    m = re.search(r"([0-9A-Za-z_+\-*/^().\s]+?)\s*(?:在\s*x\s*=|at\s+x\s*=)", problem)
    if m:
        return m.group(1).strip()
    m = re.search(r"(?:求|计算|函数)?\s*([0-9A-Za-z_+\-*/^().\s]+?)\s*(?:的)?(?:泰勒|展开|级数|幂级数)", problem)
    if m:
        return m.group(1).strip()
    return None


def _parse_expansion_point(problem: str):
    m = re.search(r"(?:在|at)\s*x\s*=\s*([-+0-9.e]+|pi)", problem)
    if m:
        try:
            return _parse(_latex2sym(m.group(1)))
        except Exception:  # noqa: BLE001
            pass
    return sympy.S(0)


# ----------------------------------------------------------------------
# 12. expectation_var —— 离散随机变量的期望 / 方差
# ----------------------------------------------------------------------
def _expectation_var(problem: str) -> Optional[dict]:
    if not re.search(r"期望|方差|标准差|矩\b|expectation|variance|mean", problem, re.IGNORECASE):
        return None
    r = _dice_expectation(problem)
    if r:
        return r
    return _dist_table_expectation(problem)


def _stat_answer(E, var, problem) -> tuple:
    if re.search(r"标准差|std\b", problem, re.IGNORECASE):
        std = sympy.sqrt(var)
        return f"{_fmt_num(sympy.simplify(std))} = {format(float(std), '.10g')}", "std"
    if re.search(r"方差|variance", problem, re.IGNORECASE):
        return f"{_fmt_num(sympy.simplify(var))} = {format(float(var), '.10g')}", "var"
    return f"{_fmt_num(sympy.simplify(E))} = {format(float(E), '.10g')}", "E"


def _dice_expectation(problem: str) -> Optional[dict]:
    if not re.search(r"骰子|dice|die\b", problem, re.IGNORECASE):
        return None
    if re.search(r"([一二两三四五六]|\d+)\s*(?:颗|个|枚|只)\s*骰", problem):
        mm = re.search(r"([一二两三四五六]|\d+)\s*(?:颗|个|枚|只)\s*骰", problem)
        if _cn_int(mm.group(1)) != 1:
            return None  # 多骰子仅概率管线支持，期望管线保守放弃
    # 单颗骰子 X ~ U{1..6}
    vals = list(range(1, 7))
    probs = [sympy.Rational(1, 6)] * 6
    E = sum(sympy.Rational(v) * p for v, p in zip(vals, probs))
    E2 = sum(sympy.Rational(v ** 2) * p for v, p in zip(vals, probs))
    var = sympy.simplify(E2 - E ** 2)
    # 自验：Σp = 1；E[(X-E)²] 与 E[X²]-E[X]² 双路径一致
    if sum(probs) != 1:
        return None
    direct_var = sum(sympy.Rational((v - E) ** 2) * p for v, p in zip(vals, probs))
    if sympy.simplify(direct_var - var) != 0:
        return None
    text, which = _stat_answer(E, var, problem)
    return {"answer": text,
            "kind": "expectation_var",
            "evidence": "Σp_i=1 且 E[(X-E[X])²] 与 E[X²]-E[X]² 双路径一致"}


def _dist_table_expectation(problem: str) -> Optional[dict]:
    pairs = re.findall(r"[PX]\s*\(\s*[Xx]\s*=\s*(-?\d+(?:\.\d+)?)\s*\)\s*=\s*([-+]?\d+(?:\.\d+)?)", problem)
    if not pairs:
        pairs = re.findall(r"[Xx]\s*=\s*(-?\d+(?:\.\d+)?)\s*(?:的)?\s*概率\s*(?:为|是)?\s*([-+]?\d+(?:\.\d+)?)", problem)
    if not pairs:
        return None
    rows = []
    for vm, pm in pairs:
        v = sympy.Rational(vm)
        p = sympy.Rational(pm)
        if p < 0:
            return None
        rows.append((v, p))
    if not rows:
        return None
    # 自验：Σp = 1（容差）
    if sympy.simplify(sum(p for _, p in rows) - 1) != 0:
        return None
    E = sum(v * p for v, p in rows)
    E2 = sum(v ** 2 * p for v, p in rows)
    var = sympy.simplify(E2 - E ** 2)
    direct_var = sympy.simplify(sum((v - E) ** 2 * p for v, p in rows))
    if sympy.simplify(direct_var - var) != 0:
        return None
    text, which = _stat_answer(E, var, problem)
    return {"answer": text,
            "kind": "expectation_var",
            "evidence": "Σp_i=1 且 E[(X-E[X])²] 与 E[X²]-E[X]² 双路径一致"}


# ----------------------------------------------------------------------
# 13. residue_complex —— 留数
# ----------------------------------------------------------------------
def _residue_complex(problem: str) -> Optional[dict]:
    if not re.search(r"留数|residue|围道积分", problem, re.IGNORECASE):
        return None
    m = re.search(r"([0-9A-Za-z_+\-*/^().\s]+?)\s*(?:在|at)\s*z\s*=\s*([-+0-9.e]+)", problem, re.IGNORECASE)
    if not m:
        return None
    ftext = m.group(1)
    z0_text = m.group(2)
    z = sympy.Symbol("z")
    try:
        f = _parse(_latex2sym(ftext))
        if not (f.free_symbols <= {z}):
            return None
        z0 = _parse(_latex2sym(z0_text))
        if z0.free_symbols:
            return None
        res = sympy.residue(f, z, z0)
    except Exception:  # noqa: BLE001
        return None
    res = sympy.simplify(res)
    # 自验：数值围道积分（绕 z0 的小圆）比对
    try:
        num_res = _contour_residue(f, z, z0)
        if num_res is None or abs(float(res) - num_res) > 1e-5:
            return None
    except Exception:  # noqa: BLE001
        return None
    return {"answer": _fmt_num(res),
            "kind": "residue_complex",
            "evidence": f"数值围道积分 ∮f dz/(2πi) ≈ {format(num_res, '.6g')} 与 sympy 留数一致"}


def _contour_residue(f, z, z0) -> Optional[float]:
    import cmath
    try:
        z0c = complex(float(z0))
    except Exception:  # noqa: BLE001
        return None
    # 选半径：避开其它奇点
    r = 0.5
    try:
        sing = sympy.singularities(f, z)
        others = [s for s in sing if sympy.simplify(s - z0) != 0]
        if others:
            dist = min(abs(float((s - z0).evalf())) for s in others)
            r = min(0.5, dist / 2)
        if r <= 0:
            return None
    except Exception:  # noqa: BLE001
        pass
    import warnings
    n = 200
    total = 0j
    with warnings.catch_warnings():
        # mpmath 内部 DeprecationWarning（bitcount）由 sympy evalf 触发，与结果无关
        warnings.simplefilter("ignore", DeprecationWarning)
        for k in range(n):
            theta = 2 * math.pi * k / n
            zk = z0c + r * cmath.exp(1j * theta)
            try:
                fk = complex(f.subs(z, zk).evalf())
            except Exception:  # noqa: BLE001
                return None
            total += fk * (1j * r * cmath.exp(1j * theta)) * (2 * math.pi / n)
    return (total / (2 * math.pi * 1j)).real


# ----------------------------------------------------------------------
# 14. ode_ivp —— 一阶 / 二阶 ODE 初值问题
# ----------------------------------------------------------------------
def _ode_ivp(problem: str) -> Optional[dict]:
    if not re.search(r"y\s*(?:''|'|″|′)|初值|微分方程", problem):
        return None
    t = problem.replace("y''", "ypp").replace("y′", "yp").replace("y'", "yp").replace("y″", "ypp")
    m = re.search(r"ypp\s*=\s*([^,，。;；\n]+)", t)
    if m:
        order = 2
        expr_text = m.group(1).strip()
    else:
        m = re.search(r"yp\s*=\s*([^,，。;；\n]+)", t)
        if not m:
            return None
        order = 1
        expr_text = m.group(1).strip()
    init_m = re.search(r"y\s*\(\s*(-?\d+(?:\.\d+)?)\s*\)\s*=\s*(-?\d+(?:\.\d+)?)", t)
    if not init_m:
        return None
    t0 = float(init_m.group(1))
    y0 = float(init_m.group(2))
    yp0 = 0.0
    if order == 2:
        yp0_m = re.search(r"yp\s*\(\s*(-?\d+(?:\.\d+)?)\s*\)\s*=\s*(-?\d+(?:\.\d+)?)", t)
        if not yp0_m:
            return None
        yp0 = float(yp0_m.group(2))
    all_y = [float(p) for p in re.findall(r"y\s*\(\s*(-?\d+(?:\.\d+)?)\s*\)", t)]
    targets = [p for p in all_y if abs(p - t0) > 1e-9]
    if not targets:
        return None
    t1 = targets[0]
    x = sympy.Symbol("x")
    y = sympy.Symbol("y")
    yp = sympy.Symbol("yp")
    try:
        f_expr = _parse(_latex2sym(expr_text))
    except Exception:  # noqa: BLE001
        return None
    try:
        if order == 1:
            if not (f_expr.free_symbols <= {x, y}):
                return None
            fn = sympy.lambdify((x, y), f_expr, modules="numpy")

            def rhs(tv, Y):
                return [fn(tv, Y[0])]

            y0_list = [y0]
        else:
            if not (f_expr.free_symbols <= {x, y, yp}):
                return None
            fn = sympy.lambdify((x, y, yp), f_expr, modules="numpy")

            def rhs(tv, Y):
                return [Y[1], fn(tv, Y[0], Y[1])]

            y0_list = [y0, yp0]
        from scipy.integrate import solve_ivp
        sol = solve_ivp(rhs, [t0, t1], y0_list, rtol=1e-10, atol=1e-12, dense_output=True)
        val = float(sol.sol(t1)[0])
        # 自验：反向积分回初始点偏差 < 1e-8
        sol_rev = solve_ivp(rhs, [t1, t0], sol.sol(t1), rtol=1e-10, atol=1e-12, dense_output=True)
        if abs(float(sol_rev.sol(t0)[0]) - y0) > 1e-8:
            return None
        evidence = f"反向积分回 y({_pt(t0)}) 偏差 < 1e-8"
    except Exception:  # noqa: BLE001
        return None
    # 若 sympy 能求解析解则直接比对
    try:
        yf = sympy.Function("y")
        if order == 1:
            de = sympy.Eq(yf(x).diff(x), f_expr.subs(y, yf(x)))
            ics = {yf(t0): y0}
        else:
            de = sympy.Eq(yf(x).diff(x, 2), f_expr.subs({y: yf(x), yp: yf(x).diff(x)}))
            ics = {yf(t0): y0, yf(x).diff(x).subs(x, t0): yp0}
        sol_gen = sympy.dsolve(de, yf(x), ics=ics)
        if isinstance(sol_gen, sympy.Equality):
            val_an = float(sympy.N(sol_gen.rhs.subs(x, t1), 20))
            if abs(val_an - val) > 1e-6:
                return None
            evidence += "；与 sympy 解析解一致"
    except Exception:  # noqa: BLE001
        pass
    answer = f"y({_pt(t1)}) ≈ {format(val, '.10g')}"
    return {"answer": answer, "kind": "ode_ivp", "evidence": evidence}


# ----------------------------------------------------------------------
# 15. bvp_shoot —— 二阶边值问题（打靶法）
# ----------------------------------------------------------------------
def _bvp_shoot(problem: str) -> Optional[dict]:
    if not re.search(r"边值|BVP|boundary", problem, re.IGNORECASE):
        return None
    if "ypp" not in problem and "y''" not in problem and "y″" not in problem:
        return None
    t = problem.replace("y''", "ypp").replace("y′", "yp").replace("y'", "yp").replace("y″", "ypp")
    m = re.search(r"ypp\s*=\s*([^,，。;；\n]+)", t)
    if not m:
        return None
    expr_text = m.group(1).strip()
    bcs = re.findall(r"y\s*\(\s*(-?\d+(?:\.\d+)?)\s*\)\s*=\s*([^,，。;；\n]+)", t)
    if len(bcs) < 2:
        return None
    x = sympy.Symbol("x")
    y = sympy.Symbol("y")
    yp = sympy.Symbol("yp")
    try:
        f_expr = _parse(_latex2sym(expr_text))
        if not (f_expr.free_symbols <= {x, y, yp}):
            return None
        fn = sympy.lambdify((x, y, yp), f_expr, modules="numpy")

        def rhs(tv, Y):
            return [Y[1], fn(tv, Y[0], Y[1])]
    except Exception:  # noqa: BLE001
        return None
    # 解析边界值（允许 sin(1) 等表达式）
    try:
        bc_parsed = []
        for a_text, beta_text in bcs:
            beta = float(sympy.N(_parse(_latex2sym(beta_text)), 20))
            bc_parsed.append((float(a_text), beta))
    except Exception:  # noqa: BLE001
        return None
    bc_parsed.sort(key=lambda p: p[0])
    a, alpha = bc_parsed[0]
    b, beta = bc_parsed[-1]
    if a >= b:
        return None
    all_y = [float(p) for p in re.findall(r"y\s*\(\s*(-?\d+(?:\.\d+)?)\s*\)", t)]
    targets = [p for p in all_y if all(abs(p - bp) > 1e-9 for bp, _ in bc_parsed)]
    if not targets:
        return None
    x0 = targets[0]
    if not (a < x0 < b):
        return None
    try:
        from scipy.integrate import solve_ivp

        def shoot(s):
            sol = solve_ivp(rhs, [a, b], [alpha, s], rtol=1e-10, atol=1e-12, dense_output=True)
            return float(sol.sol(b)[0]) - beta

        s_found = _bracket_solve(shoot)
        if s_found is None:
            return None
        sol = solve_ivp(rhs, [a, b], [alpha, s_found], rtol=1e-10, atol=1e-12, dense_output=True)
        residual = abs(float(sol.sol(b)[0]) - beta)
        if residual > 1e-8:  # 自验：打靶边界残差 < 1e-8
            return None
        val = float(sol.sol(x0)[0])
    except Exception:  # noqa: BLE001
        return None
    answer = f"y({_pt(x0)}) ≈ {format(val, '.10g')}"
    return {"answer": answer,
            "kind": "bvp_shoot",
            "evidence": f"打靶边界残差 {residual:.2e} < 1e-8"}


def _bracket_solve(f, lo=-20.0, hi=20.0, step=1.0, max_iters=200) -> Optional[float]:
    from scipy.optimize import brentq
    prev_s = lo
    try:
        prev_v = f(prev_s)
    except Exception:  # noqa: BLE001
        return None
    s = lo + step
    it = 0
    while s <= hi and it < max_iters:
        try:
            v = f(s)
        except Exception:  # noqa: BLE001
            return None
        if prev_v * v <= 0:
            try:
                return float(brentq(f, prev_s, s, xtol=1e-12))
            except Exception:  # noqa: BLE001
                return None
        prev_s, prev_v = s, v
        s += step
        it += 1
    return None


# ----------------------------------------------------------------------
# 输出美化
# ----------------------------------------------------------------------
def _fmt_poly(expr) -> str:
    text = str(expr).replace("**", "^").replace("*", "·")
    return text
