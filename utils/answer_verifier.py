"""候选答案程序化验证器（V4.9 SVC 阶段 3）。

核心思想（AIMO2 冠军方案）：验证器 > 投票器——"候选答案对不对"不靠
LLM 判断，靠**程序化检查**（代入题面条件/小规模枚举对照/数值模拟）。

可靠性设计：
- 每候选生成 **2 份独立验证代码**（不同方法：代入验证 / 枚举对照），
  验证代码也是 LLM 生成（可能错）→ 双验证缓解；
- 2 份都 PASS → 确定性锁定（verified=True, strong=True）；
- 1 份 PASS + 1 份 INCONCLUSIVE → 弱锁定（strong=False）；
- 有任何 FAIL → 不锁定（候选可能错，交给投票）；
- 执行走 MCP 沙箱（与 python_agent 同路径），60s 超时。

合规：纯 API + sympy 计算，官方允许。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("answer_verifier")

_VERIFY_PROMPT = """你是数学验证员。下面的数学题有一个**候选答案**，你的任务不是求解，
而是**用代码独立验证候选答案是否正确**。

【题目】
{problem}

【候选答案】
{candidate}

写一个完整 Python 程序验证，**必须产生独立的数值/枚举证据**（禁止只做逻辑
推理判断——"看起来合理"不算验证）：

1. **独立计算对照**：忽略候选答案，自己用暴力枚举/模拟/sympy 精确计算题面
   目标值（或小规模实例的目标值），得到**独立数值**；
2. **与候选比对**：把独立计算值与候选答案（或其对应实例值）**数值比较**
   （Fraction 精确比较或容差 1e-6）；注意候选可能是表达式/通项，需在
   具体实例下代入比较；
3. 每个实例都要有明确的"候选值 vs 独立计算值"输出，如
   print("n=1: cand=7, actual=5")。

最后必须输出一行：
- 全部实例数值吻合 → print("VERIFY: PASS")
- 任何实例不吻合（含反例） → print("VERIFY: FAIL")
- 完全无法独立计算 → print("VERIFY: INCONCLUSIVE")

**铁律**：不得因为"候选看起来有道理"而 PASS；不得用候选答案本身参与
计算（避免自证）；PASS 必须建立在独立计算数值吻合上。
只输出 ```python``` 代码块。
"""

_ALTERNATE_VERIFY_PROMPT = """你是数学验证员（第二独立方法）。下面数学题的**候选答案**需要
用**完全不同的独立方法**验证（与第一份验证代码不同的算法）。

【题目】
{problem}

【候选答案】
{candidate}

写 Python 程序验证（必须独立计算，禁止逻辑推理判断）：
- 用**反例搜索**：穷举/随机生成大量实例（参数/边界/极端值），对每个实例
  独立计算题面目标值，与候选答案（或其实例值）比较；找到任何不吻合即 FAIL；
- 或独立枚举：小规模完全枚举/模拟，独立算出答案，与候选数值比较；
- 每个实例打印对照：print("实例: cand=..., actual=...")。

最后输出：
- 全部吻合 → print("VERIFY: PASS")
- 找到反例 → print("VERIFY: FAIL")
- 无法独立计算 → print("VERIFY: INCONCLUSIVE")

**铁律**：不得用候选答案参与计算；PASS 必须基于独立计算数值吻合。
只输出 ```python``` 代码块。
"""


def _extract_code(resp: str) -> str:
    m = re.search(r"```python[ \t]*\n(.*?)\n```", resp or "", re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*\n?(.*?)\n?```", resp or "", re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


def _parse_verdict(stdout: str) -> str:
    """判定验证结果。V5.0-fix：PASS 必须附带数值对照证据——
    验证代码须打印 cand/actual（或候选/实际）对照行且含数字，否则视为
    INCONCLUSIVE（防"无对照也 PASS"的同源确认偏误，#0/#7 实证错锁）。"""
    m = re.search(r"VERIFY:\s*(PASS|FAIL|INCONCLUSIVE)", stdout or "")
    if not m:
        return "INCONCLUSIVE"
    verdict = m.group(1)
    if verdict == "PASS":
        # 证据要求：stdout 里必须有对照证据（cand/actual/候选/实际/比较 + 数字）
        evidence = re.search(
            r"(?i)(cand|actual|候选|实际|对照|比较|expected|期望).{0,40}\d"
            r"|\d.{0,40}(cand|actual|候选|实际|对照|比较|expected|期望)",
            stdout or "")
        if not evidence:
            return "INCONCLUSIVE"
    return verdict


def _run_verify_code(client, deps, problem: str, candidate: str,
                     prompt: str, label: str) -> str:
    """生成验证代码 → MCP 执行 → 返回 verdict（PASS/FAIL/INCONCLUSIVE）。"""
    from utils.llm_retry import chat_prefilled, thinking_mode_flag
    from utils.cot_stripper import strip_cot_prefix
    resp = chat_prefilled(
        client,
        messages=[{"role": "user", "content": prompt}],
        prefix="```python\n",
        temperature=0.2,
        max_tokens=4096,
        logger=deps.logger,
        time_budget=deps.time_budget,
        label=label,
        thinking_mode=thinking_mode_flag(),
    )
    code = _extract_code(resp or "")
    if not code:
        return "INCONCLUSIVE"
    mcp = deps.mcp_client
    if mcp is None:
        return "INCONCLUSIVE"
    output = mcp.execute(code, timeout=60)
    return _parse_verdict(str(output.get("stdout") or ""))


def verify_candidate(client, deps, problem: str, candidate: str,
                     config=None) -> dict:
    """双独立验证候选答案。返回 {"verified": bool, "strong": bool, "verdicts": [...]}。

    verified=True 表示候选通过程序化验证（可确定性锁定）；
    strong=True 表示 2 份独立验证都 PASS（最高置信）。

    V5.0-fix：仅对**闭式数值/表达式候选**启用验证——文本/逻辑答案
    （No/Yes/策略描述）验证器不可靠（同源确认偏误，#0/#7 实证错锁），
    直接返回未验证（交给投票兜底）。
    """
    candidate = str(candidate or "").strip()[:400]
    if not candidate:
        return {"verified": False, "strong": False, "verdicts": []}
    try:
        from utils.rule_judge import is_closed_form_answer
        if not is_closed_form_answer(candidate):
            return {"verified": False, "strong": False, "verdicts": [],
                    "skipped": "non_closed_answer"}
    except Exception:  # noqa: BLE001 - 判定失败按不可验证处理
        return {"verified": False, "strong": False, "verdicts": []}
    p1 = _VERIFY_PROMPT.format(problem=str(problem)[:5000], candidate=candidate)
    p2 = _ALTERNATE_VERIFY_PROMPT.format(problem=str(problem)[:5000],
                                         candidate=candidate)
    verdicts = []
    try:
        verdicts.append(_run_verify_code(client, deps, problem, candidate,
                                         p1, "verify_primary"))
        verdicts.append(_run_verify_code(client, deps, problem, candidate,
                                         p2, "verify_alt"))
    except Exception as exc:  # noqa: BLE001 - 验证失败不锁定
        logger.warning("verify_candidate failed: %s", exc)
        return {"verified": False, "strong": False, "verdicts": verdicts}
    strong = verdicts.count("PASS") >= 2
    weak = verdicts.count("PASS") >= 1 and "FAIL" not in verdicts
    return {
        "verified": strong or weak,
        "strong": strong,
        "verdicts": verdicts,
    }
