import json
import os
import time
from typing import Any, Dict, List, Mapping, Optional, Union

import requests

from config import CONFIG


DEFAULT_API_BASE = "https://chat.intern-ai.org.cn/api/v1/chat/completions"
DEFAULT_MODEL = CONFIG["model"]
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 4096

ChatMessage = Dict[str, Any]
ChatResponse = Union[str, ChatMessage]


class InternChatClient:
    """Small OpenAI-compatible chat client for the competition sample.

    对齐官方 baseline 最新版（commit "fix: forward Chat API request arguments"）：
    支持 ``thinking_mode`` / ``tools`` / ``**request_args`` 透传；有 tool_calls 时
    返回 message dict（而非 content str），供工具调用循环读取。仅用于本地调试——
    官方评测时 client 由平台注入，本文件不参与正式判分。
    """

    def __init__(
        self,
        timeout: int = 120,
        retry: int = 3,
        default_args: Optional[Mapping[str, Any]] = None,
        model: Optional[str] = None,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        **request_args: Any,
    ) -> None:
        # V3.3 多模型复核通道（路径 B）：允许显式指定 model/api_base/api_key
        # 构造第二 Intern-S 模型的 client；不传时行为与官方 baseline 完全一致。
        raw_api_key = api_key or os.environ.get("INTERN_API_KEY")
        if not raw_api_key:
            raise RuntimeError("Missing API key. Set INTERN_API_KEY.")
        self.authorization = (
            raw_api_key if raw_api_key.startswith("Bearer ") else f"Bearer {raw_api_key}"
        )
        self.api_base = api_base or os.environ.get("INTERN_API_BASE", DEFAULT_API_BASE)
        self.model = model or os.environ.get("INTERN_MODEL", DEFAULT_MODEL)
        self.timeout = timeout
        self.retry = retry
        self.default_args = dict(default_args or {})
        self.default_args.update(request_args)

    def chat(
        self,
        messages: List[ChatMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        *,
        thinking_mode: Optional[bool] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        **request_args: Any,
    ) -> ChatResponse:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": DEFAULT_TEMPERATURE,
            "max_tokens": DEFAULT_MAX_TOKENS,
        }
        payload.update(self.default_args)
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if thinking_mode is not None:
            payload["thinking_mode"] = thinking_mode
        if tools is not None:
            payload["tools"] = tools
        payload.update(request_args)
        # messages 是唯一每次必传的参数，不能被 default_args 意外覆盖。
        payload["messages"] = messages

        headers = {
            "Content-Type": "application/json",
            "Authorization": self.authorization,
        }

        last_error = None
        for attempt in range(self.retry):
            try:
                response = requests.post(
                    self.api_base,
                    headers=headers,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
                message = data["choices"][0]["message"]
                # 有工具调用时返回整个 message dict（含 tool_calls）；否则返回纯文本，
                # 与历史调用方保持向后兼容。
                if "tool_calls" in message:
                    return message
                return message["content"]
            except Exception as exc:  # noqa: BLE001 - keep sample robust and simple.
                last_error = exc
                if attempt + 1 < self.retry:
                    time.sleep(2**attempt)

        raise RuntimeError(f"Chat completion failed after {self.retry} attempts: {last_error}")
