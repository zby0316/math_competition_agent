"""V3.0 python-first 决策节点（solver_decision）。

python_agent 跑完后，本节点判断"python 的确定性结果是否足以降级推理"：
- python 成功 + 证据 support/pass + 答案完整 + 非近似算法题
  → 置 ``reasoning_light=True``：推理路径降为单次独立复核（省采样投票/
    B 路径* /重试/压缩通道），把预算还给证明题；
- 否则 → 空返回，走完整推理路径（现状不变）。

安全边界（宁可多花一次调用，不冒确定性错误）：
- 近似算法题（梯形/辛普森/数值积分）python 会算"精确值"而非题面要求的
  近似值（真机实证），绝不 light；
- evidence 必须 support/pass（python 自检通过）；placeholder/残片答案不 light；
- light 只影响**推理深度**，交叉验证、Critic、仲裁、代回核验等全部保留——
  light 推理若与 python 不一致，仍走 mismatch 全套补救链。
"""

from __future__ import annotations

import re

from config import CONFIG
from utils.cot_stripper import is_placeholder_answer
from utils.problem_profile import is_objective_mode

#: 近似算法信号：python 会算精确值而非题面要求的近似值 → 不 light
_APPROX_RE = re.compile(r"梯形|辛普森|Simpson|数值积分|近似(?:计算|值)?")
#: 答案可用性下限（字符）
_MIN_ANSWER_CHARS = 1
#: 证据必须通过的状态
_OK_EVIDENCE = {"support", "pass"}


def solver_decision_node(state, config):
    """返回 {"reasoning_light": True} 或 {}。纯确定性判断，零 LLM。"""
    if not CONFIG.get("enable_python_first", True):
        return {}
    if is_objective_mode(state.get("question_mode", "computation")):
        # 客观题快速路径自带复核机制（revote/judge_confirm），不走 light
        return {}
    problem = str(state.get("problem") or "")
    if _APPROX_RE.search(problem):
        return {}
    po = state.get("python_output") or {}
    if not po.get("success"):
        return {}
    answer = str(po.get("answer") or "").strip()
    if not answer or is_placeholder_answer(answer):
        return {}
    if po.get("evidence_status") not in _OK_EVIDENCE:
        return {}
    # python 确定性成功 → 推理降级为独立复核
    return {"reasoning_light": True}
