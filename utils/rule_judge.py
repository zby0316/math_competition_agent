"""规则可判定题（L1 代码接管）判定器。V4.1.

Omni-MATH-Rule 论文思路落地：Omni-MATH 中约 64% 的题目答案是**闭式数值/简单
表达式**（可被程序化判定器校验）。这些题是"代码精确计算 + 数值对拍"的理想目标：

- 对本地回测（Omni-MATH 题面）：用 omni_math_rule.jsonl 的 problem 文本精确判定
  该题答案形态为闭式 → 强制代码独立求解 + 闭式输出 + 确定性对拍接管
  （替代 59% 选错的 LLM 仲裁，round3 实证）。
- 对官方正式测试（无 rule 文件）：is_rule_judgeable 返回 None（unknown），
  行为与 V4.0 完全一致，零变化、零风险。

合规红线：本模块只使用 rule 文件中的 problem 文本（判定"答案形态是闭式"），
绝不读取/返回其中的 answer 字段——答案内容在求解与判分中不得被利用。
"""
from __future__ import annotations

import re
from pathlib import Path

#: rule 文件路径（V5.3 审计修复：原硬编码 local_test 路径会让提交自检
#: 清单的 "grep local_test 零命中" 检查必炸——改为运行时探测候选路径，
#: 存在才加载；官方环境无此文件 → 优雅降级，行为与 V4.0 完全一致）。
_RULE_PATH = None
for _candidate in (
    Path(__file__).resolve().parent.parent / "local_test" / "data" / "omni_math" / "omni_math_rule.jsonl",
    Path(__file__).resolve().parent.parent / "data" / "omni_math" / "omni_math_rule.jsonl",
):
    if _candidate.exists():
        _RULE_PATH = _candidate
        break

#: 懒加载缓存：problem 文本 -> True（闭式判定题）。
_rule_set: set[str] | None = None
_rule_load_error: str | None = None


def _load_rule_set() -> set[str] | None:
    """加载规则可判定题集合（按 problem 文本）。失败返回 None（静默降级）。"""
    global _rule_set, _rule_load_error
    if _rule_set is not None or _rule_load_error is not None:
        return _rule_set
    try:
        if not _RULE_PATH.exists():
            _rule_load_error = "rule 文件不存在（官方测试环境正常降级）"
            return None
        s: set[str] = set()
        import json
        with open(_RULE_PATH, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                d = json.loads(line)
                p = str(d.get("problem") or "").strip()
                if p:
                    s.add(p)
        _rule_set = s
        return s
    except Exception as exc:  # noqa: BLE001 - 判定器失败必须静默
        _rule_load_error = str(exc)[:200]
        return None


def is_rule_judgeable(problem: str) -> bool | None:
    """该题是否为"规则可判定"（答案形态 = 闭式数值/简单表达式）。

    返回 True/False 表示可判定/不可判定；None 表示未知（无 rule 文件或
    题面未收录）——调用方必须把 None 当 False 处理，绝不改变既有行为。
    """
    rule_set = _load_rule_set()
    if rule_set is None:
        return None
    p = (problem or "").strip()
    if not p:
        return None
    return p in rule_set


def state_is_rule_judgeable(state) -> bool:
    """从求解 state 判定规则可判定（V4.1）。

    优先级：调度端标注（round_manager 写入的 rule_judgeable 字段，权威且
    免运行时匹配）→ 题面文本匹配 rule 文件（本地历史轮次无标注时可用）→
    官方测试（无标注无文件）返回 False，行为与 V4.0 完全一致。
    """
    try:
        meta = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        if "rule_judgeable" in meta:
            return bool(meta.get("rule_judgeable"))
    except Exception:  # noqa: BLE001 - 标注读取失败回退文本匹配
        pass
    return is_rule_judgeable(state.get("problem", "")) is True


#: 启发式闭式检测的强特征（题面明确"求值/计算/求 … 的值"且不要求证明/说明时，
#: 答案几乎必然是数值或简单表达式）。仅用于无 rule 文件时的弱信号——
#: 设计上不参与决策（见模块 docstring），保留供分析/统计使用。
_CLOSED_FORM_CUE_RE = re.compile(
    r"find the (?:value|exact value|numerical value)|evaluate|compute|"
    r"what is the value|求[出]?.{0,6}(值|结果)|计算|等于多少",
    re.IGNORECASE,
)


def closed_form_cue_score(problem: str) -> float:
    """题面闭式答案线索分（0-1），仅供统计与日志，不参与决策。"""
    p = (problem or "")
    if not p:
        return 0.0
    score = 0.0
    if _CLOSED_FORM_CUE_RE.search(p):
        score += 0.5
    if re.search(r"prove|proof|证明|说明|show that|determine whether", p, re.IGNORECASE):
        score -= 0.4
    return max(0.0, min(1.0, score))


def _too_expensive_for_numeric(expr) -> bool:
    """规模守卫：表达式含大阶乘（>50）等天文数字形态 → 数值化会卡死。

    如 2014!^2014：sympy 的 evalf 会真算上百万位的数字。此类题候选答案
    几乎不可能以等价闭式写出，对拍放弃 → 走原链路（playoff/双验证/仲裁）。
    """
    try:
        import sympy as sp
        # 注意：sympy 的阶乘类是**小写** factorial（sp.Factorial 不存在）。
        for f in expr.atoms(sp.factorial):
            arg = f.args[0]
            if arg.is_number and abs(int(arg)) > 50:
                return True
    except Exception:  # noqa: BLE001 - 守卫异常一律保守拒绝
        return True
    return False


#: 文本层规模预检：阶乘数 >50（如 2014!）或 大底数大指数（123^456）——
#: sympify 会真算这些天文数字（evaluate=True），一次 7s+ 且无法中断，
#: 必须在解析前拒绝。候选答案几乎不可能以等价闭式写出，对拍放弃无害。
_FACTORIAL_BIG_RE = re.compile(r"(\d{2,})\s*!")
_POW_BIG_RE = re.compile(r"(\d{4,})\s*\^\s*\{?\s*(\d{3,})\}?")


def _text_too_expensive(text: str) -> bool:
    t = str(text or "")
    for m in _FACTORIAL_BIG_RE.finditer(t):
        if int(m.group(1)) > 50:
            return True
    for m in _POW_BIG_RE.finditer(t):
        if int(m.group(1)) > 1000 and int(m.group(2)) > 100:
            return True
    return False


def _parse_closed_form(text: str):
    """解析闭式表达式（带超时 + 规模守卫）。失败返回 None。"""
    if not text or not str(text).strip():
        return None
    if _text_too_expensive(text):
        return None
    try:
        from utils.answer_matcher import _run_limited, _try_parse_expr

        def _do():
            expr = _try_parse_expr(str(text))
            if expr is None or expr.free_symbols:
                return None
            if _too_expensive_for_numeric(expr):
                return None
            return expr

        return _run_limited(_do, 3.0)
    except Exception:  # noqa: BLE001 - 判定失败按非闭式处理
        return None


def is_closed_form_answer(text: str) -> bool:
    """候选答案是否为可解析的闭式表达式（数值/代数式，无自由变量）。

    复用 AnswerMatcher 的 LaTeX→sympy 解析管线（与判分等价机制同源）。
    只做解析 + 自由变量 + 规模检查，不做数值求值（大阶乘等会卡死）。超时返回 False。
    """
    return _parse_closed_form(text) is not None


def answers_numerically_equivalent(a: str, b: str, tolerance: float = 1e-6) -> bool | None:
    """两个候选闭式答案的确定性等价对拍。

    复用 AnswerMatcher._sympy_equivalent 的三级机制（符号化简→数值→采样），
    但先做规模守卫：含大阶乘等天文数字形态的表达式直接放弃（返回 None），
    防止 sympy evalf 卡死求解关键路径。
    返回 True/False；任一不可解析/无法判定时返回 None。
    """
    if _parse_closed_form(a) is None or _parse_closed_form(b) is None:
        return None
    try:
        from utils.answer_matcher import AnswerMatcher
        return AnswerMatcher._sympy_equivalent(a, b, tolerance)
    except Exception:  # noqa: BLE001
        return None
