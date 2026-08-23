"""官方 client 响应归一化（VeritasMath 平台防线，移植自启元 MathAgent 实战教训）。

启元真机教训（其 DELIVERY_REPORT/README 实录）：平台注入的 client.chat 返回值
不一定是 str——可能是 OpenAI choices dict、content blocks 数组、bytes、
嵌套 message 或带 content 属性的对象。基线方案默认返回 str 处理，
一旦形态不符，下游所有解析静默失败 → 整批 0 分。

本模块在 LLMRetryWrapper 单点收口：任何 client 返回值先归一化为 str 再进图。
"""

from __future__ import annotations

import weakref

#: chat 调用形态缓存（2026-08-23 合规收敛）：官方要求不读取/修改注入 client
#: 的内部属性，历史版本把探测结果 setattr 到 client 对象（_veritas_chat_mode）
#: 违反该要求。改为模块级弱引用缓存——不触碰 client 实例，且 client 被回收
#: 时缓存自动失效。
_CHAT_MODE_CACHE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _blocks_text(blocks) -> str:
    """content blocks 数组 → 纯文本（{"type":"text","text":...} 形态）。"""
    if isinstance(blocks, str):
        return blocks
    if isinstance(blocks, bytes):
        try:
            return blocks.decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return ""
    if not isinstance(blocks, list):
        return ""
    parts = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, bytes):
            try:
                parts.append(block.decode("utf-8", "ignore"))
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(block, dict):
            text = block.get("text") or block.get("content") or ""
            if isinstance(text, str):
                parts.append(text)
            elif isinstance(text, bytes):
                try:
                    parts.append(text.decode("utf-8", "ignore"))
                except Exception:  # noqa: BLE001
                    pass
    return "".join(parts)


def normalize_chat_response(resp) -> str:
    """把任意形态的 client.chat 返回值归一化为纯文本。永不抛异常。"""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, bytes):
        try:
            return resp.decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return ""
    if isinstance(resp, list):
        return _blocks_text(resp)
    if isinstance(resp, dict):
        try:
            choices = resp.get("choices") or []
            if choices and isinstance(choices[0], dict):
                message = choices[0].get("message") or choices[0]
                text = _blocks_text(message.get("content"))
                if text:
                    return text
        except Exception:  # noqa: BLE001
            pass
        for key in ("content", "text", "answer"):
            text = _blocks_text(resp.get(key))
            if text.strip():
                return text
            value = resp.get(key)
            if isinstance(value, dict):  # 嵌套 message 形态
                nested = _blocks_text(value.get("content") or value.get("text"))
                if nested.strip():
                    return nested
        if "tool_calls" in resp:  # 工具调用形态：序列化供下游提取层兜底
            try:
                import json
                return json.dumps(resp, ensure_ascii=False)[:2000]
            except Exception:  # noqa: BLE001
                return ""
        return ""
    content = getattr(resp, "content", None)  # AgentMessage 等对象形态
    text = _blocks_text(content)
    if text:
        return text
    try:
        return str(resp)
    except Exception:  # noqa: BLE001
        return ""


def detect_truncation(resp) -> bool:
    """检测 client 返回是否因 max_tokens 被截断（finish_reason == "length"）。

    V2.4 官方评测日志实证：truncated_count=91（10.2% 请求）答案没写完就被切断，
    而 normalize_chat_response 只取 content、丢弃 finish_reason——这 91 个截断
    答案被当作完整答案返回，下游无法感知也无法补救。此函数补上这个信号，
    供 LLMRetryWrapper 在截断时触发续写补全。

    兼容 dict（OpenAI 形态 choices[0].finish_reason）与对象（finish_reason 属性）。
    """
    try:
        if isinstance(resp, dict):
            choices = resp.get("choices") or []
            if choices and isinstance(choices[0], dict):
                return choices[0].get("finish_reason") == "length"
            return resp.get("finish_reason") == "length"
        return getattr(resp, "finish_reason", None) == "length"
    except Exception:  # noqa: BLE001 - 检测失败按未截断处理，不影响主流程
        return False


def chat_compatible(client, messages, temperature, max_tokens, thinking_mode=None):
    """签名探测调用（VeritasMath 平台防线）。

    平台 client 的 chat 签名未知：可能只接受 messages，或不接受关键字参数。
    按 关键字三参 → 位置三参 → 仅 messages 三级降级探测；探测结果缓存在
    模块级 WeakKeyDictionary（2026-08-23 合规收敛：不再 setattr 到注入的
    client 对象上——官方要求"不要读取/修改 client 内部属性"，我们自己的
    缓存也不应触碰官方 client 实例），避免每次调用都付一次 TypeError。

    ``thinking_mode``（V2.6 截断根治）：官方原生关深度思考（长 CoT）的开关，
    仅在 keyword 模式下透传（官方 client 的 chat 签名里它是 keyword-only）。
    降级到 positional/messages_only 时该开关自然丢弃——那些 client 本就不支持。
    """
    try:
        mode = _CHAT_MODE_CACHE.get(client)
    except Exception:  # noqa: BLE001 - 不可哈希 client 直接走全量探测
        mode = None

    def _kwargs():
        kw = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if thinking_mode is not None:
            kw["thinking_mode"] = thinking_mode
        return client.chat(**kw)

    attempts = {
        "kwargs": _kwargs,
        "positional": lambda: client.chat(messages, temperature, max_tokens),
        "messages_only": lambda: client.chat(messages),
    }
    if mode in attempts:
        try:
            return attempts[mode]()
        except TypeError:
            pass  # 缓存模式失效（平台热更新等），重新全量探测
    last_error = None
    for name in ("kwargs", "positional", "messages_only"):
        try:
            result = attempts[name]()
            try:
                _CHAT_MODE_CACHE[client] = name
            except Exception:  # noqa: BLE001 - 不可哈希 client 不缓存
                pass
            return result
        except TypeError as exc:
            last_error = exc
            continue
    # 三种形态都 TypeError：不是签名问题，是 client 内部故障，原样抛出
    raise last_error  # type: ignore[misc]
