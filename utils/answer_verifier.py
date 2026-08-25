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
而是**用代码验证候选答案是否正确**。

【题目】
{problem}

【候选答案】
{candidate}

请写一个完整 Python 程序验证：
1. **代入验证**：把候选答案代回题面条件（方程/约束/关系式），用 sympy/Fraction
   精确检查是否成立（等式两边数值相等/表达式化简为真）；
2. **小规模枚举对照**（若题面含参数 n/一般情形）：取 n=1,2,3（或最小可行实例），
   暴力枚举/直接计算该实例的正确答案，与候选答案在对应实例下的值对照；
3. 若题面是概率/期望题：用蒙特卡洛模拟（≥10000 次）对照候选值（容差 1e-2）。

最后必须输出一行：print("VERIFY: PASS")（验证通过）
或 print("VERIFY: FAIL")（候选答案错误，附反例）
或 print("VERIFY: INCONCLUSIVE")（无法验证，不要硬判）。

只输出 ```python``` 代码块。
"""

_ALTERNATE_VERIFY_PROMPT = """你是数学验证员（第二独立方法）。下面数学题的**候选答案**需要
用**完全不同的方法**验证（与第一次验证代码不同的算法思路）。

【题目】
{problem}

【候选答案】
{candidate}

写 Python 程序验证（与第一份验证代码用不同方法）：
- 若第一份用了代入，这里用**独立小规模枚举**或**反例搜索**（随机/穷举找候选
  答案的反例，如随机参数代回检查不等式/等式）；
- 或用 sympy 独立推导候选答案的等价形式，与给定候选比对；
- 或对博弈/构造题做完整状态搜索（BFS/回溯）核对候选结论。

最后必须输出一行：print("VERIFY: PASS") / print("VERIFY: FAIL") / print("VERIFY: INCONCLUSIVE")。
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
    m = re.search(r"VERIFY:\s*(PASS|FAIL|INCONCLUSIVE)", stdout or "")
    return m.group(1) if m else "INCONCLUSIVE"


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
    """
    candidate = str(candidate or "").strip()[:400]
    if not candidate:
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
