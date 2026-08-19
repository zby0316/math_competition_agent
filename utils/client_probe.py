"""官方 client 探针（VeritasMath）：探测注入 client 的真实契约，写进 trace。

背景（官方评测实证）：
    本地 ``llm_client.InternChatClient`` 与官方 lagent 注入的 client 行为大概率
    不一致。V2.4 官方 truncated_count=91、V2.4 强化版 truncated_count=116 的根因
    嫌疑是同一件事的两面：
      1. ``response_normalize.chat_compatible`` 三级降级若落到 ``messages_only``，
         ``max_tokens`` 被丢弃，官方 client 用**自己的默认上限**（远小于我们配的
         12288/16384），硬题四章节写一半就被 finish_reason=length 掐断；
      2. ``prefill`` 抑制 CoT 依赖 backend 尊重 trailing assistant turn
         （continue / echo / ignore 三种），官方若是 ignore，私有 reasoning_content
         回来塞满 max_tokens，答案一个字都没写出来。

本模块零/低成本探测以下事实，供下一轮按"官方真相"调参，不再盲区瞎调：
    - chat 签名（接受哪些参数、keyword 还是 positional）→ 回答"max_tokens 传不传得进去"
    - 返回形态（str/dict/object/list/bytes）→ 回答"finish_reason 看不看得见"
    - finish_reason 是否可见 → 回答"截断检测信号①能不能用"
    - prefill（trailing assistant turn）是否被接受 → 回答"抑制 CoT 是否生效"
    - 主流程实际落到哪种调用模式（client._veritas_chat_mode）→ 实锤 max_tokens 是否丢失

所有探测都在 try/except 内，失败不影响主流程；结果写入 trace 的 client_probe 段。
"""

from __future__ import annotations

import inspect


def _extract_text(resp) -> str:
    """把任意形态的响应尽力转成纯文本（探针自用，不依赖 response_normalize）。"""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, bytes):
        try:
            return resp.decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return ""
    if isinstance(resp, dict):
        choices = resp.get("choices") or []
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or choices[0]
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content)
        for key in ("content", "text", "answer"):
            val = resp.get(key)
            if isinstance(val, str):
                return val
        return ""
    if isinstance(resp, list):
        return "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in resp)
    content = getattr(resp, "content", None)
    if content is not None:
        return _extract_text(content)
    try:
        return str(resp)
    except Exception:  # noqa: BLE001
        return ""


def _extract_finish_reason(resp):
    """尽力抽取 finish_reason；拿不到返回 None（区分"不可见"与"值是 stop"）。"""
    try:
        if isinstance(resp, dict):
            choices = resp.get("choices") or []
            if choices and isinstance(choices[0], dict):
                return choices[0].get("finish_reason")
            return resp.get("finish_reason")
        return getattr(resp, "finish_reason", None)
    except Exception:  # noqa: BLE001
        return None


def probe_signature(client) -> dict:
    """零成本探测：client.chat 签名 + client 属性。不发任何 API 调用。

    这是最高价值的一步：``inspect.signature`` 直接回答"max_tokens/temperature
    到底传不传得进去"，且零 token、零时间、零超时风险。
    """
    report = {
        "client_type": type(client).__name__,
        "has_chat": False,
        "chat_signature": "",
        "chat_params": [],
        "accepts_max_tokens": None,
        "accepts_temperature": None,
        "accepts_var_kwargs": False,
    }
    chat = getattr(client, "chat", None)
    if chat is None:
        return report
    report["has_chat"] = True
    try:
        sig = inspect.signature(chat)
        report["chat_signature"] = str(sig)
        params = []
        for p in sig.parameters.values():
            params.append(p.name)
            if p.name == "max_tokens":
                report["accepts_max_tokens"] = p.kind in (
                    inspect.Parameter.KEYWORD_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD)
            if p.name == "temperature":
                report["accepts_temperature"] = p.kind in (
                    inspect.Parameter.KEYWORD_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD)
            if p.kind == inspect.Parameter.VAR_KEYWORD:
                report["accepts_var_kwargs"] = True
        report["chat_params"] = params
    except (TypeError, ValueError) as exc:
        # builtin / C 扩展函数常无 __signature__
        report["chat_signature"] = f"unavailable ({type(exc).__name__})"
    for attr in ("timeout", "retry", "model", "temperature", "max_tokens"):
        try:
            report[f"attr_{attr}"] = getattr(client, attr, None)
        except Exception:  # noqa: BLE001
            report[f"attr_{attr}"] = None
    return report


def probe_contract(client) -> dict:
    """低成本探测：各发一次最小调用，探测返回形态 / finish_reason / prefill。

    只应在首次评测做一次（调用方用类级缓存保证），每次调用 max_tokens 极小、
    提示词极短，成本 ~1-2s / 几个 token。失败不抛异常，只记录错误形态。
    """
    report = {
        "plain_return_type": "",
        "plain_finish_reason": "",
        "finish_reason_visible": False,
        "plain_sample": "",
        "prefill_return_type": "",
        "prefill_starts_with_seed": None,
        "prefill_sample": "",
    }
    # 1. 普通调用：返回形态 + finish_reason（这是截断检测信号①是否可用的关键）。
    try:
        raw = client.chat([{"role": "user", "content": "只回复数字 1，不要任何解释。"}])
        report["plain_return_type"] = type(raw).__name__
        fr = _extract_finish_reason(raw)
        if fr is not None:
            report["finish_reason_visible"] = True
            report["plain_finish_reason"] = str(fr)
        report["plain_sample"] = _extract_text(raw)[:120]
    except Exception as exc:  # noqa: BLE001
        report["plain_return_type"] = f"error:{type(exc).__name__}"
        report["plain_finish_reason"] = str(exc)[:120]
    # 2. prefill 调用：trailing assistant turn 是否被接受（continue/echo/ignore）。
    try:
        raw2 = client.chat([
            {"role": "user", "content": "计算 1+1，直接给结果。"},
            {"role": "assistant", "content": "答案："},
        ])
        report["prefill_return_type"] = type(raw2).__name__
        text = _extract_text(raw2)
        stripped = text.lstrip()
        report["prefill_starts_with_seed"] = stripped.startswith("答案：")
        report["prefill_sample"] = stripped[:200]
    except Exception as exc:  # noqa: BLE001
        report["prefill_return_type"] = f"error:{type(exc).__name__}"
        report["prefill_sample"] = str(exc)[:120]
    return report


def read_chat_mode(client) -> dict:
    """读主流程实际落到哪种调用模式（chat_compatible 缓存的结果）。

    如果值是 ``messages_only``，即实锤"max_tokens 被丢弃、官方用默认上限"；
    如果值是 ``kwargs``/``positional``，说明 max_tokens 传进去了。
    仅在主流程至少跑过一次 chat 后才有值，故放到 trace 阶段再读。
    """
    return {
        "chat_mode": getattr(client, "_veritas_chat_mode", None),
    }
