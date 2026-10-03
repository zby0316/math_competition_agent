import json
import os
import threading
import time
from typing import Dict, List

import requests

from config import CONFIG


DEFAULT_API_BASE = "https://chat.intern-ai.org.cn/api/v1/chat/completions"
DEFAULT_MODEL = CONFIG["model"]


class InternChatClient:
    """Small AI Platform-compatible chat client for the competition sample.

    chat() 保持返回字符串（与平台官方 client 对齐，不破坏 solve 契约），同时把
    finish_reason / usage / reasoning_content 记录到实例属性，让上层能识别
    "服务端因 max_tokens 截断"的真实停止原因，而不是靠正文末尾猜（gbq 版提分
    的关键之一就是读了 finish_reason 做截断判定 + 断点续写）。

    并发安全：finish_reason 等元信息同时写入线程本地存储 ``self._tls``——20 路
    并发共享同一个 client 时，实例属性会被其它线程覆盖，只有线程本地才能保证
    LLMRetryGuard 在同一次调用返回后读到自己的真实停止原因。
    """

    def __init__(
        self,
        timeout: int = 1300,
        retry: int = 3,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str | None = None,
    ) -> None:
        raw_api_key = api_key if api_key is not None else os.environ.get("INTERN_API_KEY")
        if not raw_api_key:
            raise RuntimeError("Missing АРI keys. Set INTERN_API_KEY.")
        self.authorization = (
            raw_api_key if raw_api_key.startswith("Bearer ") else f"Bearer {raw_api_key}"
        )
        self.api_base = api_base or os.environ.get("INTERN_API_BASE", DEFAULT_API_BASE)
        self.model = model or os.environ.get("INTERN_MODEL", DEFAULT_MODEL)
        self.record_dir = None
        self.timeout = timeout
        self.retry = retry
        # 最近一次成功调用的传输层元信息，供上层做截断判定（本地调试用）。
        self.last_finish_reason = None
        self.last_usage = None
        self.last_reasoning_content = None
        # 线程本地存储：并发下每个调用线程读到自己的元信息。
        self._tls = threading.local()

    def for_problem(self, idx):
        if self.record_dir is None:
            return self
        from utils.call_recording import RecordedClient
        return RecordedClient(self, self.record_dir, idx)

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": self.authorization,
        }

        last_error = None
        self._tls.finish_reason = None
        self._tls.usage = None
        self._tls.reasoning_content = ""
        self._tls.http_attempts = 0
        for attempt in range(self.retry):
            try:
                self._tls.http_attempts += 1
                response = requests.post(
                    self.api_base,
                    headers=headers,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
                choice = data["choices"][0]
                message = choice.get("message", {})
                finish_reason = choice.get("finish_reason")
                usage = data.get("usage") or {}
                reasoning_content = (message.get("reasoning_content") or "").strip()
                self.last_finish_reason = finish_reason
                self.last_usage = usage
                self.last_reasoning_content = reasoning_content
                # 线程本地副本：并发共享 client 时实例属性会被覆盖，上层必须
                # 走 _tls 才能读到本次调用的真实停止原因。
                self._tls.finish_reason = finish_reason
                self._tls.usage = usage
                self._tls.reasoning_content = reasoning_content
                content = message.get("content") or ""
                if isinstance(content, list):
                    content = "".join(part.get("text", "") if isinstance(part, dict) else str(part)
                                      for part in content)
                return str(content).strip()
            except Exception as exc:  # noqa: BLE001 - keep sample robust and simple.
                last_error = exc
                if attempt + 1 < self.retry:
                    time.sleep(2**attempt)

        raise RuntimeError(f"Chat completion failed after {self.retry} attempts: {last_error}")
