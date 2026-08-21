# -*- coding: utf-8 -*-
"""第二 Intern-S 模型复核通道（V3.3）。

规则依据（官方知识库 FAQ，约 3113-3116 行）：
    "可以使用 Intern-S 系列模型，不限于 Intern-S1……选手可以按需选择或组合多个
    Intern-S 系列模型，一般来说新模型的数学能力会更强。"

    "不可以在正式评测运行时调用其他在线 AI 模型或外部 API……运行时只允许访问
    官方提供的 Intern-S 系列模型 API。"（约 3127-3130 行）

    "正式评测时平台会使用来自书生 API 控制台的官方 client，不使用选手自带
    API key。"（约 494 行）

因此复核通道只在两种合法且可用的条件下激活，任一不满足即静默降级回主 client
（与单模型行为完全一致，零风险）：

    路径 A（官方评测环境自动激活，零配置）：
        官方注入 client 的 chat 接受 ``model=`` 逐调用覆盖（OpenAI-compatible），
        包一层 ModelOverrideClient 把复核调用指向 CONFIG["review_model"]
        （默认 intern-s1-pro）。走平台配额与网关，不依赖选手 key。

    路径 B（本地调试显式开启）：
        环境变量 INTERN_REVIEW_MODEL 非空 且 INTERN_API_KEY 存在时，
        用 llm_client.InternChatClient 直连书生 API 控制台做复核。
        不设 INTERN_REVIEW_MODEL 就不建——避免选手本地 shell 常驻的
        INTERN_API_KEY 让单元测试误触真实网络。

使用纪律：
    - 复核只出现在**分歧路径**（语义仲裁选择候选、客观题低置信复核投票），
      不在主解题链路——主链路仍由主模型（s2-preview）负责；
    - 每题复核调用总数受 CONFIG["review_max_calls_per_problem"] 封顶；
    - 复核调用失败/模型名不支持/配额用尽 → 立即回退主 client 同参数调用，
      绝不让分歧路径因复核通道而丢失仲裁机会。
"""

from __future__ import annotations

import inspect
import os
from typing import Any, Dict, List, Optional

from config import CONFIG


class ReviewClientUnsupported(RuntimeError):
    """复核 client 无法按预期工作（模型参数不支持等），调用方应回退主 client。"""


class _ReviewBudget:
    """每题复核调用配额（挂在 resolved client 实例上，天然按题隔离）。"""

    def __init__(self, max_calls: int):
        self.max_calls = max(0, int(max_calls))
        self.used = 0

    def available(self) -> int:
        return self.max_calls - self.used

    def try_acquire(self) -> bool:
        if self.used >= self.max_calls:
            return False
        self.used += 1
        return True


def _chat_accepts_model(client: Any) -> bool:
    """client.chat 是否接受 model 关键字（显式形参或 **kwargs）。

    结果缓存在 client 对象上；inspect 失败时保守返回 False（宁可不用复核，
    也不在主流程上冒 TypeError）。逐调用覆盖是路径 A 的前提。
    """
    if client is None:
        return False
    cached = getattr(client, "_veritas_accepts_model", None)
    if cached is not None:
        return bool(cached)
    result = False
    try:
        sig = inspect.signature(getattr(client, "chat", None))
        params = sig.parameters
        result = "model" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    except Exception:  # noqa: BLE001 - C 扩展/绑定方法 inspect 失败按不支持处理
        result = False
    try:
        setattr(client, "_veritas_accepts_model", result)
    except Exception:  # noqa: BLE001 - 只读对象不设缓存
        pass
    return result


def _main_model_name(client: Any) -> str:
    """尽力读主 client 的模型名，用于跳过"同名复核"（纯浪费）。"""
    for attr in ("model", "default_model", "_model", "model_name"):
        try:
            value = getattr(client, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        except Exception:  # noqa: BLE001
            continue
    return ""


class ModelOverrideClient:
    """路径 A：把官方 client 包装成"逐调用带 model= 的复核 client"。

    chat 的形态探测镜像 utils.response_normalize.chat_compatible 的三级降级，
    但多一步：首选带 ``model=`` 的关键字调用。落到"不带 model 的关键字/位置
    参数"模式说明底层根本不支持模型覆盖——立即标记 unsupported 并抛出
    ReviewClientUnsupported，调用方回退主 client（否则会假装换了模型，
    实为同模型重复采样，丧失复核的独立性意义）。
    """

    def __init__(self, client: Any, model: str):
        self._client = client
        self.model = model
        self.review_model_name = model
        self.review_budget: Optional[_ReviewBudget] = None
        self.unsupported = False
        self._mode: Optional[str] = None

    def chat(
        self,
        messages: List[Dict],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        *,
        thinking_mode: Optional[bool] = None,
        **kwargs: Any,
    ) -> Any:
        if self.unsupported:
            raise ReviewClientUnsupported(
                f"underlying client does not accept model override ({self.model})")

        def _kw_call(with_model: bool):
            kw: Dict[str, Any] = {"messages": messages}
            if with_model:
                kw["model"] = self.model
            if temperature is not None:
                kw["temperature"] = temperature
            if max_tokens is not None:
                kw["max_tokens"] = max_tokens
            if thinking_mode is not None:
                kw["thinking_mode"] = thinking_mode
            kw.update(kwargs)
            return self._client.chat(**kw)

        def _pos_call():
            return self._client.chat(messages, temperature, max_tokens)

        attempts = {
            "kwargs_model": lambda: _kw_call(True),
            "kwargs_nomodel": lambda: _kw_call(False),
            "positional": _pos_call,
            "messages_only": lambda: self._client.chat(messages),
        }
        if self._mode in attempts:
            try:
                return attempts[self._mode]()
            except TypeError:
                pass  # 缓存模式失效，重新全量探测
        last_error: Optional[BaseException] = None
        for name in ("kwargs_model", "kwargs_nomodel", "positional", "messages_only"):
            try:
                result = attempts[name]()
                self._mode = name
                if name != "kwargs_model":
                    # 模型覆盖没有真正生效 → 复核通道名存实亡，禁用以免误导
                    self.unsupported = True
                    raise ReviewClientUnsupported(
                        f"chat fell back to {name} mode: model override not applied")
                return result
            except TypeError as exc:
                last_error = exc
                continue
            except ReviewClientUnsupported:
                raise
        raise last_error  # type: ignore[misc]


def resolve_review_client(main_client: Any, logger=None) -> Optional[Any]:
    """为本题解析复核 client；不可用返回 None（静默降级，零异常外溢）。

    每题调用一次：返回的实例自带独立调用配额（review_budget），天然按题隔离。
    """
    try:
        if not CONFIG.get("enable_review_model", True):
            return None
        max_calls = int(CONFIG.get("review_max_calls_per_problem", 3) or 0)
        if max_calls <= 0:
            return None
        review_model = (
            os.environ.get("INTERN_REVIEW_MODEL")
            or str(CONFIG.get("review_model") or "")
        ).strip()
        if not review_model:
            return None
        main_model = _main_model_name(main_client)
        if main_model and main_model == review_model:
            if logger:
                logger.info("review model %s == main model, review channel off",
                            review_model)
            return None
        resolved: Optional[Any] = None
        # 路径 A：官方 client 支持 model 逐调用覆盖（官方评测环境的合法组合方式）
        if _chat_accepts_model(main_client):
            resolved = ModelOverrideClient(main_client, review_model)
        # 路径 B：显式 INTERN_REVIEW_MODEL 才自建 client（本地调试专用）
        elif os.environ.get("INTERN_REVIEW_MODEL") and os.environ.get("INTERN_API_KEY"):
            try:
                from llm_client import InternChatClient
                resolved = InternChatClient(model=review_model)
            except Exception as exc:  # noqa: BLE001 - key 缺失/网络不通等，回退主 client
                if logger:
                    logger.warning("env review client unavailable: %s", exc)
                resolved = None
        if resolved is not None:
            resolved.review_budget = _ReviewBudget(max_calls)
            resolved.review_model_name = review_model
            if logger:
                logger.info(
                    "review channel ON: model=%s path=%s max_calls=%s",
                    review_model, type(resolved).__name__, max_calls)
        return resolved
    except Exception as exc:  # noqa: BLE001 - 复核通道绝不外溢异常
        if logger:
            logger.warning("resolve_review_client failed, degraded: %s", exc)
        return None


def review_client(deps) -> Optional[Any]:
    """取 deps 上的复核 client；未配置/已无配额时返回 None。"""
    rc = getattr(deps, "review_client", None)
    if rc is None:
        return None
    budget = getattr(rc, "review_budget", None)
    if budget is not None and budget.available() <= 0:
        return None
    return rc


def review_client_name(deps) -> Optional[str]:
    rc = review_client(deps)
    return getattr(rc, "review_model_name", None) if rc is not None else None


def _fallback_log(logger, label: str, exc: Optional[BaseException]) -> None:
    if logger:
        logger.warning(
            "%s: review-model call failed (%s), falling back to main client",
            label, exc)


def chat_with_retry_review(
    deps,
    messages,
    temperature=0.2,
    max_tokens=4096,
    logger=None,
    time_budget=None,
    expected_call_seconds=None,
    label="llm_review",
    thinking_mode=None,
) -> str:
    """复核模型重试调用；失败/无配额回退主 client 同参数调用。"""
    from utils.llm_retry import chat_with_retry
    rc = review_client(deps)
    budget = getattr(rc, "review_budget", None) if rc is not None else None
    if rc is None or budget is None or not budget.try_acquire():
        return chat_with_retry(
            deps.client, messages, temperature, max_tokens, logger,
            time_budget, expected_call_seconds, label, thinking_mode)
    try:
        return chat_with_retry(
            rc, messages, temperature, max_tokens, logger,
            time_budget, expected_call_seconds, label + "_review", thinking_mode)
    except Exception as exc:  # noqa: BLE001 - 复核失败必须保住主链路裁决机会
        _fallback_log(logger, label, exc)
        return chat_with_retry(
            deps.client, messages, temperature, max_tokens, logger,
            time_budget, expected_call_seconds, label, thinking_mode)


def chat_prefilled_review(
    deps,
    messages,
    prefix,
    temperature=0.1,
    max_tokens=96,
    logger=None,
    time_budget=None,
    expected_call_seconds=None,
    label="llm_prefill_review",
    max_retries=1,
    reserve_margin_s=None,
    thinking_mode=None,
) -> str:
    """复核模型 prefill 调用；失败/无配额回退主 client 同参数调用。"""
    from utils.llm_retry import chat_prefilled
    rc = review_client(deps)
    budget = getattr(rc, "review_budget", None) if rc is not None else None
    if rc is None or budget is None or not budget.try_acquire():
        return chat_prefilled(
            deps.client, messages, prefix, temperature, max_tokens, logger,
            time_budget, expected_call_seconds, label, max_retries,
            reserve_margin_s, thinking_mode)
    try:
        return chat_prefilled(
            rc, messages, prefix, temperature, max_tokens, logger,
            time_budget, expected_call_seconds, label + "_review", max_retries,
            reserve_margin_s, thinking_mode)
    except Exception as exc:  # noqa: BLE001 - 同上
        _fallback_log(logger, label, exc)
        return chat_prefilled(
            deps.client, messages, prefix, temperature, max_tokens, logger,
            time_budget, expected_call_seconds, label, max_retries,
            reserve_margin_s, thinking_mode)
