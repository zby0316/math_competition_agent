"""传输层重试，受单题截止时间约束而非尝试次数约束。

为什么只数次数不够：客户端由平台注入，`InternChatClient` 内部已经在 120s
套接字超时下重试 3 次。外层再试 3 次，最多 9 个请求、约 1080s，全砸进一个
节点——足以单独击穿单题 20 分钟上限，某道题那次 1307s 超时就是耗在这里。
内部次数降不了（属于平台客户端），所以每次尝试都必须先问时钟要许可。
"""

import logging
import time
from typing import Dict, List

from config import CONFIG
from utils.seed_prompt import prefill_messages, stitch


class ChatResult(str):
    """携带传输层元信息的字符串：finish_reason / reasoning_content / usage。

    作为 ``str`` 子类，len()、切片、正则、startswith 等一切字符串操作照常，
    所有调用点无需改动；只在需要截断判定处读 ``.finish_reason``。这补上了
    此前"记录了 finish_reason 却因并发共享 client 读不到、等于盲飞"的缺口。
    """

    def __new__(cls, text, finish_reason=None, reasoning_content="", usage=None):
        obj = super().__new__(cls, text if isinstance(text, str) else str(text or ""))
        obj.finish_reason = finish_reason
        obj.reasoning_content = reasoning_content or ""
        obj.usage = usage
        return obj


class DeadlineExceeded(RuntimeError):
    """当单题时间预算已无法负担下一次尝试时抛出。"""


class LLMRetryGuard:
    def __init__(
        self,
        client,
        max_retries: int = 3,
        backoff_factor: float = 2.0,
        logger=None,
        time_budget=None,
        expected_call_seconds: float | None = None,
        safeguard_margin_s: float | None = None,
    ):
        self.client = client
        self.max_retries = max(1, max_retries)
        self.backoff_factor = backoff_factor
        self.logger = logger or logging.getLogger("math_agent")
        self.time_budget = time_budget
        self.expected_call_seconds = expected_call_seconds
        # safeguard_margin_s: 允许本次调用动用 safeguard（硬上限前的预留时间），但必须
        # 给后续的答案落盘留出 margin 秒。曾发现大量题目的仲裁因软预算耗尽被整体
        # 跳过，而仲裁恰恰是最需要复核的场景。仲裁的 prefill 调用只要 ~10s，
        # 从 safeguard(300s) 划出固定配额是净收益。
        self.safeguard_margin_s = safeguard_margin_s

    def _now(self) -> float:
        """读取预算自己的时钟，让实测耗时与剩余时间一致。

        这里若混用 `time.monotonic()`，而预算用的是别的时钟，两者不一致时观测
        时长就失去意义，等于静默禁用了本应阻止注定失败重试的自适应检查。
        """
        now = getattr(self.time_budget, "now", None)
        return now() if callable(now) else time.monotonic()

    def _affordable(self, observed: float | None = None) -> bool:
        """时钟能否再负担一次尝试？

        `observed` 是上一次尝试实际花掉的时间。它很重要：一次传输故障烧掉
        363s（客户端自己的 3 × 120s 重试），在同样的负载下几乎还会再烧 363s，
        若按乐观的 `expected_call_seconds` 估算重试，就会批准一个根本完不成的
        调用。实测耗时永远优先于估算。
        """
        if not self.time_budget:
            return True
        estimate = self.expected_call_seconds
        if observed is not None:
            estimate = observed if estimate is None else max(estimate, observed)
        if self.safeguard_margin_s is not None:
            # safeguard 配额模式：软预算已尽也允许，只要硬上限前还能容下
            # 本次调用 + margin。remaining_hard 不足时照样拒绝。
            window = self.time_budget.remaining_hard() - float(self.safeguard_margin_s)
            return window >= (estimate if estimate is not None else 1.0)
        if estimate is None:
            return not self.time_budget.expired()
        return self.time_budget.can_afford(estimate)

    def chat(
        self,
        messages: List[Dict],
        temperature: float = 0.2,
        max_tokens: int = 4096,
        label: str = "llm",
    ) -> str:
        if not self._affordable():
            need = (f"~{self.expected_call_seconds:.0f}s"
                    if self.expected_call_seconds is not None else "any time at all")
            raise DeadlineExceeded(
                f"{label}: {self.time_budget.remaining():.0f}s left, need {need}"
            )

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            started = self._now()
            try:
                result = self.client.chat(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                if self.time_budget:
                    self.time_budget.record(label, self._now() - started, model_call=True,
                        http_attempts=getattr(getattr(self.client, "_tls", None), "http_attempts", None))
                # 从客户端线程本地读本次调用的真实停止原因（平台 client 无
                # _tls 时优雅降级为 None，ChatResult 仍是普通字符串）。
                finish_reason = None
                reasoning_content = ""
                usage = None
                tls = getattr(self.client, "_tls", None)
                if tls is not None:
                    finish_reason = getattr(tls, "finish_reason", None)
                    reasoning_content = getattr(tls, "reasoning_content", "") or ""
                    usage = getattr(tls, "usage", None)
                return ChatResult(
                    result, finish_reason=finish_reason,
                    reasoning_content=reasoning_content, usage=usage,
                )
            except Exception as exc:  # noqa: BLE001 - 重试瞬时传输故障。
                last_error = exc
                observed = self._now() - started
                if self.time_budget:
                    self.time_budget.record(f"{label}:failed", observed, model_call=True,
                        http_attempts=getattr(getattr(self.client, "_tls", None), "http_attempts", None))
                if attempt >= self.max_retries:
                    break
                # 只有当时钟还能负担一次与上次同样耗时的调用，重试才值得买。
                # 否则尽快失败，让调用方趁还有时间走到兜底。
                if not self._affordable(observed):
                    self.logger.warning(
                        "%s: 放弃第 %s/%s 次重试（已耗时 %.0fs），时间预算无法 "
                        "负担下一次尝试（%s）",
                        label, attempt, self.max_retries, observed, exc,
                    )
                    break
                delay = 0 if self.backoff_factor == 0 else self.backoff_factor ** (attempt - 1)
                self.logger.warning(
                    "LLM 调用在第 %s/%s 次尝试失败：%s", attempt, self.max_retries, exc
                )
                if delay > 0:
                    time.sleep(delay)
        raise last_error


def chat_with_retry(
    client,
    messages,
    temperature=0.2,
    max_tokens=4096,
    logger=None,
    time_budget=None,
    expected_call_seconds=None,
    label="llm",
):
    return LLMRetryGuard(
        client,
        max_retries=CONFIG["llm_max_retries"],
        backoff_factor=CONFIG["backoff_factor"],
        logger=logger,
        time_budget=time_budget,
        expected_call_seconds=expected_call_seconds,
    ).chat(
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        label=label,
    )


def chat_prefilled(
    client,
    messages,
    prefix,
    temperature=0.1,
    max_tokens=96,
    logger=None,
    time_budget=None,
    expected_call_seconds=None,
    label="llm_prefill",
    max_retries=1,
    safeguard_margin_s=None,
):
    """一次预填往返，返回拼接后的助手文本。

    默认单次尝试：预填调用是廉价的快路径，且每个调用方背后都有全预算的非预填
    兜底，在这里花重试次数只会买延迟而不买可靠性。

    ``safeguard_margin_s`` 非 None 时，本调用允许动用 safeguard（软预算已尽仍可
    执行），仅要求硬上限前剩余 ≥ 调用估时 + margin。用于仲裁保底与应急直答。
    """
    wrapper = LLMRetryGuard(
        client,
        max_retries=max_retries,
        backoff_factor=CONFIG["backoff_factor"],
        logger=logger,
        time_budget=time_budget,
        expected_call_seconds=expected_call_seconds,
        safeguard_margin_s=safeguard_margin_s,
    )
    raw = wrapper.chat(
        messages=prefill_messages(messages, prefix),
        temperature=temperature,
        max_tokens=max_tokens,
        label=label,
    )
    # stitch 返回普通 str，会丢掉 finish_reason；重新包回 ChatResult 保留元信息。
    stitched = stitch(prefix, raw)
    return ChatResult(
        stitched,
        finish_reason=getattr(raw, "finish_reason", None),
        reasoning_content=getattr(raw, "reasoning_content", "") or "",
        usage=getattr(raw, "usage", None),
    )
