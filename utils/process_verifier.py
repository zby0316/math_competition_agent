# -*- coding: utf-8 -*-
"""过程验证器 PV（V2.5.7）：逐步骤推理链逻辑验证 + n 次并行采样置信度。

移植 Intern-S1-MO 论文（arXiv:2512.10739 §3 "Verifying Theorems to Mitigate
Error Propagation"）的过程验证思想，与既有 Critic（结果级：契约完整性 +
中间计算抽核）互补——PV 是**过程级**：逐步骤检查"每一步能否由题面 + 上一步
正确推出"。

设计原则（对齐论文 + 本项目"机制净升才上"铁律）：
- **n 次并行采样多数票**：单次验证有假阳性/假阴性，n=3（温度 0.3/0.7/1.0）
  并行短调用，按步骤取多数票，抵御单次误判（论文：parallel sampling +
  confidence score）。
- **触发面窄（自适应预算）**：只在 hard / 冲突 / 低置信时启用，medium 快速
  路径不触发——简单题一轮过，难题才多轮验证（论文：adaptive reasoning budget）。
- **只审计不改写**：返回漏洞清单，由调用方（critic 门）决定是否触发定向修复；
  PV 本身不产生新答案。
- **失败不阻塞**：任何异常/超时/解析失败 → 返回空结果，主链路照走。

输出：
    {"overall": "sound|flawed|unverifiable",
     "flawed_steps": [{"step": n, "flaw": "..."}],
     "samples": n, "unverifiable_reason": "..."}
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from config import CONFIG
from utils.llm_retry import chat_prefilled
from utils.prompt_templates import PV_PREFILL, PV_PROMPT, PV_SYSTEM_PROMPT
from utils.token_budget import estimate_tokens

#: 采样数（论文 n=4，我们用 3 控制成本；多数票阈值 2/3）
_PV_SAMPLES_DEFAULT = 3
_PV_MAX_STEPS = 10          # 步骤过多时只审前 N 步（成本与注意力控制）
_PV_MAX_TOKENS = 1024
_PV_CALL_ESTIMATE_S = 20    # 单次短验证调用预算
_PV_RESERVE_MARGIN_S = 60   # 触发前必须保留的墙钟余量


def _parse_verdict(raw: str) -> dict | None:
    """从 prefill/普通回复解析 JSON 判定；解析失败返回 None。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001
        parsed = None
    if not isinstance(parsed, dict):
        # 容错：从文本中提取第一个完整 JSON 对象
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            try:
                parsed = json.loads(text[start:end + 1])
            except Exception:  # noqa: BLE001
                parsed = None
    if not isinstance(parsed, dict):
        return None
    verdicts = parsed.get("step_verdicts")
    if not isinstance(verdicts, list):
        return None
    steps = []
    for item in verdicts:
        if not isinstance(item, dict):
            continue
        try:
            step_num = int(item.get("step", 0))
        except (TypeError, ValueError):
            step_num = 0
        valid = item.get("valid")
        severity = str(item.get("severity") or "").strip().lower()
        steps.append({
            "step": step_num,
            "valid": valid is True,
            "severity": severity if severity in ("critical", "minor") else "minor",
            "flaw": str(item.get("flaw") or "")[:200],
        })
    overall = str(parsed.get("overall") or "").strip().lower()
    # severity 优先：模型可能报 overall=flawed 但漏洞全为 minor——
    # 按"是否存在 critical 无效步骤"重算，避免 minor 表述问题升级（真机实证 教训）。
    has_critical = any(not s["valid"] and s["severity"] == "critical" for s in steps)
    if has_critical:
        overall = "flawed"
    elif steps:
        overall = "sound" if overall != "unverifiable" else "unverifiable"
    else:
        overall = "unverifiable"
    return {"step_verdicts": steps, "overall": overall}


def _steps_text(state: dict) -> str:
    """从 reasoning_result 提取逐步骤文本（PV 的输入）。"""
    rr = state.get("reasoning_result") or {}
    steps = rr.get("steps") or []
    analysis = str(rr.get("analysis") or "").strip()
    lines = []
    if analysis:
        lines.append(f"【推理分析】{analysis[:1200]}")
    for s in steps[: _PV_MAX_STEPS]:
        num = s.get("step_num", "?")
        desc = str(s.get("description") or "").strip()
        if desc:
            lines.append(f"步骤{num}: {desc[:900]}")
    return "\n".join(lines) or "（无推理步骤记录）"


def _pv_call(deps, state, answer: str, temperature: float) -> dict | None:
    """单次 PV 采样调用。失败返回 None（不阻塞）。"""
    prompt = PV_PROMPT.format(
        problem=str(state.get("problem") or "")[:4000],
        steps_text=_steps_text(state),
        answer=str(answer or "")[:2000],
    )
    try:
        raw = chat_prefilled(
            deps.client,
            messages=[
                {"role": "system", "content": PV_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            prefix=PV_PREFILL,
            temperature=temperature,
            max_tokens=_PV_MAX_TOKENS,
            logger=deps.logger,
            time_budget=deps.time_budget,
            expected_call_seconds=_PV_CALL_ESTIMATE_S,
            label=f"pv_sample_{temperature}",
            reserve_margin_s=_PV_RESERVE_MARGIN_S,
        )
    except Exception as exc:  # noqa: BLE001 - 验证失败不阻塞主链路
        deps.logger.warning("PV sample (t=%.1f) unavailable: %s", temperature, exc)
        return None
    if deps.token_budget:
        deps.token_budget.consume(estimate_tokens(prompt), estimate_tokens(raw))
    return _parse_verdict(raw)


def run_process_verifier(deps, state, answer: str, samples_override: int | None = None) -> dict:
    """PV 主入口：n 次并行采样 → 按步骤多数票 → 汇总漏洞。

    返回 {"overall", "flawed_steps", "samples", "reason"}：
      - overall=sound：全部步骤多数票有效（或仅 minor 表述问题）；
      - overall=flawed：存在步骤被多数票判 **critical** 无效（含具体漏洞）；
      - overall=unverifiable：采样不足/步骤过少无法判定（不阻塞）。

    severity 分级（2026-08-23 干跑实证）：只有 critical 才判 flawed——
    minor（表述不清/简写）不升级，避免对本来就对的答案触发无谓重算。

    samples_override：仲裁辅助证据场景传 1（单次短调用；仲裁只需要
    "推理链是否成立"的参考信号，不需要多数票）。默认取 CONFIG["pv_samples"]。
    """
    if not answer or not state.get("reasoning_result"):
        return {"overall": "unverifiable", "flawed_steps": [],
                "samples": 0, "reason": "无推理链或答案"}
    clock = deps.time_budget
    if clock and clock.remaining_hard() - _PV_RESERVE_MARGIN_S < _PV_CALL_ESTIMATE_S:
        return {"overall": "unverifiable", "flawed_steps": [],
                "samples": 0, "reason": "时间预算不足"}
    n = int(samples_override if samples_override is not None
            else CONFIG.get("pv_samples", _PV_SAMPLES_DEFAULT))
    n = max(1, min(n, 3))
    # 温度列表：1 采样 (0.5)；2 采样 (0.3, 0.7)；3 采样 (0.3, 0.7, 1.0)
    temps = [0.5] if n == 1 else ([0.3, 0.7] if n == 2 else [0.3, 0.7, 1.0])
    results = []
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(_pv_call, deps, state, answer, t) for t in temps]
        for f in futures:
            try:
                results.append(f.result())
            except Exception:  # noqa: BLE001 - 单次采样失败忽略
                results.append(None)

    parsed = [r for r in results if r]
    if not parsed:
        return {"overall": "unverifiable", "flawed_steps": [],
                "samples": 0, "reason": "全部采样失败"}
    # 按步骤聚合多数票（仅统计 critical 无效票；minor 无效不计数）
    step_votes: dict[int, list[bool]] = {}
    for r in parsed:
        for s in r.get("step_verdicts") or []:
            if s["step"] > 0:
                is_critical_invalid = (not s["valid"]) and s.get("severity") == "critical"
                step_votes.setdefault(s["step"], []).append(is_critical_invalid)
    flawed = []
    for step, votes in sorted(step_votes.items()):
        invalid = sum(1 for v in votes if v)  # votes 存的是"是否 critical 无效"
        # n=1 时单次即判；n=2 时 2/2 一致；n=3 时 ≥2/3
        threshold = 1 if len(votes) == 1 else max(2, len(votes) - (len(votes) // 2))
        if invalid >= threshold:
            flaw = ""
            for r in parsed:
                for s in r.get("step_verdicts") or []:
                    if (s["step"] == step and not s["valid"]
                            and s.get("severity") == "critical" and s["flaw"]):
                        flaw = s["flaw"]
                        break
                if flaw:
                    break
            flawed.append({"step": step, "flaw": flaw or "推理步骤存在关键逻辑漏洞"})
    overall = "flawed" if flawed else "sound"
    reason = f"{len(parsed)}/{n} 次采样，{len(step_votes)} 步可判"
    return {"overall": overall, "flawed_steps": flawed,
            "samples": len(parsed), "reason": reason}
