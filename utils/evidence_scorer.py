"""确定性证据评分：候选答案之间的取舍交给可审计的证据分，而不是票数或文风。

移植自 multiface 72.32 的证据打分表（``polyhedron_core/decision_evidence.py``，
其源口径是"禁止多数票、按证据排序"），按本管线的证据来源适配：

    +3.0 tool_verified         该候选由真实运行的代码独立算出并自证
    +2.0 substitution_verified 该候选被代回原题条件/方程验证成立
    +1.5 covers_all_cases      "求所有解"型：全部分支均已枚举
    +1.0 blind_agreement       盲目独立复核（不看既有答案、换方法路径）与之一致
    +0.5 answer_shape_valid    形状有效（非空、非占位、非 nan/inf）
    -2.0 counterexample_found  该候选被反例证伪
    -1.5 condition_mismatch    与题面条件矛盾（计数/概率/长度为负等）

与 multiface 的差异：不含 ``reference_answer_copied``（本管线没有参考解可抄），
不含 ``independent_of_reference``（两条分支本就独立于检索）。

设计约束（历史教训，见 score-history 记忆）：
* 只做**取舍**，不改写任何候选文本；
* 只在证据不对称时生效，评分持平一律回落到原有选择逻辑，确保行为变化可归因；
* 全部输入来自既有产物（执行器 stdout 旗标 + 证据契约 + 题面/答案的确定性检查），
  **不新增任何 LLM 调用**。
"""

from __future__ import annotations

import math
import re

_FLAG_LINE_RE = re.compile(r"验证旗标\s*[:：]\s*(.+)")
_KV_RE = re.compile(r"([A-Za-z_]+)\s*=\s*([^,，;；\s]+)")
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_TRUTHY = {"1", "true", "yes", "y", "on", "是"}
_FALSY = {"0", "false", "no", "n", "off", "na", "n/a", "无", "否"}

#: 题面明确要求"计数/数量/概率/长度"时，负值答案必错（如"个数 = -3"）。
#: 只在这类语义词出现时启用，避免误伤本身可以取负的题目（温度、偏移、整数解）。
_NONNEGATIVE_PROBLEM_RE = re.compile(
    r"(个数|数量|种数|个数为|多少种|多少个数|概率|count|number of|how many|"
    r"probability|cardinality)",
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(r"^\s*(?:\[?result\]?|\*+|待定|未知|n/?a|-)\s*$", re.IGNORECASE)


def _flag_value(raw: str) -> bool | None:
    """把旗标值解析成 True/False；无法识别（含 na）返回 None。"""
    token = str(raw or "").strip().casefold()
    if token in _TRUTHY:
        return True
    if token in _FALSY:
        return False
    return None


def parse_python_flags(stdout: str) -> dict:
    """从 Python 分支 stdout 解析结构化验证旗标。

    约定输出恰好一行：``验证旗标: passed=<候选值>, substitution=0/1, all_cases=0/1, counterexample=0/1``
    解析不出（旧版代码、截断、模型未遵守契约）时返回空 dict——调用方按"无旗标"处理。
    """
    match = _FLAG_LINE_RE.search(stdout or "")
    if not match:
        return {}
    flags: dict = {}
    for key, value in _KV_RE.findall(match.group(1)):
        key = key.strip().casefold()
        if key == "passed":
            flags["passed"] = value.strip()
            continue
        parsed = _flag_value(value)
        if parsed is not None:
            flags[key] = parsed
    return flags


def _normalize(text: str) -> str:
    text = str(text or "").strip()
    text = re.sub(r"\\boxed\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"[\s$。．.,，;；]+", "", text)
    return text.casefold()


def _same_answer(a: str, b: str) -> bool:
    """便宜判定：归一化后相等；不等时退回 AnswerCompare 的等价判定。"""
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    try:
        from utils.answer_compare import AnswerCompare

        ok, _conf, _reason = AnswerCompare.match_computation_answer(a, b)
        return bool(ok)
    except Exception:  # noqa: BLE001 - 等价判定失败按"不同"处理最保守
        return False


def _shape_valid(answer: str) -> bool:
    text = str(answer or "").strip()
    if not text or _PLACEHOLDER_RE.match(text):
        return False
    lowered = text.casefold()
    if "nan" in lowered or "inf" in lowered:
        return False
    return True


def _condition_mismatch(problem: str, answer: str) -> bool:
    """确定性条件检查：计数/概率语义的题面下出现负数或非有限值。"""
    text = str(answer or "")
    if not text.strip():
        return False
    lowered = text.casefold()
    if "nan" in lowered or "inf" in lowered:
        return True
    if not _NONNEGATIVE_PROBLEM_RE.search(problem or ""):
        return False
    numbers = [float(item) for item in _NUMBER_RE.findall(text)]
    if not numbers:
        return False
    # 只有"全部数值字面量都为负"才判矛盾：混合表达式（如 -1/8 的推导片段、
    # 坐标 (x-3) 之类）不构成确定性矛盾证据。
    return all(value < 0 for value in numbers) and not math.isclose(
        max(numbers), 0.0, abs_tol=1e-12
    )


def collect_flags(state: dict, match_result: dict | None = None) -> dict:
    """按候选归集证据旗标。

    返回 ``{"reasoning": {...}, "python": {...}}``，每项形如
    ``{"tool_verified": bool, "substitution_verified": bool, "covers_all_cases": bool,
    "answer_shape_valid": bool, "counterexample_found": bool, "condition_mismatch": bool}``。

    归集规则（全部来自既有产物，无新增调用）：
    * ``evidence_status == "support"``：Python 分支的独立计算支持了推理候选 →
      该候选记 substitution_verified；``"contradict"``（含真实反例）→ 该候选记
      counterexample_found。这两个语义由 utils/verdict_evidence.py 保证。
    * 结构化旗标 ``passed=<值>`` 指明检查作用于哪个候选：substitution=1 给该候选
      记 substitution_verified（Python 候选的 tool_verified 也来自这里——只有它
      自己算出的答案被代回验证过，才算"工具证实"）；all_cases=1 记覆盖完备；
      counterexample=1 记被证伪。
    * 形状与条件矛盾由本模块确定性判定。
    """
    problem = str(state.get("problem") or "")
    reasoning_answer = str((state.get("reasoning_result") or {}).get("answer") or "")
    python_output = state.get("python_output") or {}
    python_answer = str(python_output.get("answer") or "")
    evidence_status = str(python_output.get("evidence_status") or "")
    stdout = str(python_output.get("stdout") or "")
    flags = parse_python_flags(stdout)
    # 反伪造闸门：静态检查判定"无实质计算却自称验证"时，模型自报的旗标一律作废
    # （否则一段只打印 counterexample=0/substitution=1 的代码就能凭空拿到证据分）。
    if (python_output.get("authenticity") or {}).get("fabricated"):
        flags = {}
    passed = str(flags.get("passed") or "")
    passed_reasoning = bool(passed) and _same_answer(passed, reasoning_answer)
    passed_python = bool(passed) and _same_answer(passed, python_answer)

    def blank() -> dict:
        return {
            "tool_verified": False,
            "substitution_verified": False,
            "covers_all_cases": False,
            "blind_agreement": False,
            "answer_shape_valid": False,
            "counterexample_found": False,
            "condition_mismatch": False,
            "evidence_score": 0.0,
        }

    result = {"reasoning": blank(), "python": blank()}

    result["reasoning"]["answer_shape_valid"] = _shape_valid(reasoning_answer)
    result["python"]["answer_shape_valid"] = _shape_valid(python_answer)

    if evidence_status == "support":
        result["reasoning"]["substitution_verified"] = True
    elif evidence_status == "contradict" and reasoning_answer:
        result["reasoning"]["counterexample_found"] = True

    if flags.get("substitution"):
        if passed_python:
            result["python"]["tool_verified"] = True
        elif passed_reasoning:
            result["reasoning"]["substitution_verified"] = True
    if flags.get("all_cases"):
        if passed_python:
            result["python"]["covers_all_cases"] = True
        elif passed_reasoning:
            result["reasoning"]["covers_all_cases"] = True
    if flags.get("counterexample"):
        if passed_python:
            result["python"]["counterexample_found"] = True
        elif passed_reasoning:
            result["reasoning"]["counterexample_found"] = True

    for side, answer in (("reasoning", reasoning_answer), ("python", python_answer)):
        if answer and _condition_mismatch(problem, answer):
            result[side]["condition_mismatch"] = True

    # 盲目独立复核（第三方证据）与哪一侧一致，哪一侧得盲复核一致分；与两侧都
    # 不同则不奖励任何一侧（分歧本身留在仲裁里处理，不做投票）。
    recheck_answer = str((state.get("recheck_result") or {}).get("answer") or "")
    if recheck_answer:
        if reasoning_answer and _same_answer(recheck_answer, reasoning_answer):
            result["reasoning"]["blind_agreement"] = True
        if python_answer and _same_answer(recheck_answer, python_answer):
            result["python"]["blind_agreement"] = True

    for side in result:
        result[side]["evidence_score"] = evidence_score(result[side])
    return result


def evidence_score(flags: dict) -> float:
    """证据打分表（权重与 multiface 一致，便于跨方案对照）。"""
    score = 0.0
    score += 3.0 if flags.get("tool_verified") else 0.0
    score += 2.0 if flags.get("substitution_verified") else 0.0
    score += 1.5 if flags.get("covers_all_cases") else 0.0
    score += 1.0 if flags.get("blind_agreement") else 0.0
    score += 0.5 if flags.get("answer_shape_valid") else 0.0
    score -= 2.0 if flags.get("counterexample_found") else 0.0
    score -= 1.5 if flags.get("condition_mismatch") else 0.0
    return score


def order_by_evidence(candidates) -> list:
    """按证据分降序排序（保持与 multiface 同名接口，便于对照实现）。"""
    ranked = []
    for candidate in candidates:
        item = dict(candidate)
        item["evidence_score"] = evidence_score(item)
        ranked.append(item)
    return sorted(ranked, key=lambda item: -item["evidence_score"])
