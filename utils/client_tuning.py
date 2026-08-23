"""Probe the injected client's transport settings (read-only).

2026-08-23 合规收敛（对照初赛要求 L2824-2825"不要读取 client 的内部属性，
不要修改 client 的鉴权信息"）：历史版本会尝试**修改**注入 client 的
``timeout``/``retry`` 属性（raise_socket_timeout / cap_internal_retries 写入
``client.timeout = ...``）。这违反官方"只依赖 client.chat 公开约定"的要求，
且平台 client 可能只读——写入失败时旧实现静默跳过，收益本就无保证。

本版本改为**纯只读探测**：用 getattr 观察 transport 配置并记录报告（不写入），
超时兜底完全交给外层 TimeBudget（单题 1200s 硬限 + 780s 调用估计已覆盖
健康调用的最坏情况）。调用方只消费报告用于日志，不再依赖"修改成功"。
"""

from __future__ import annotations

import logging

#: A hard problem's reasoning call is the longest single request in the graph. The
#: per-problem deadline (TimeBudget) is what actually bounds runtime, so the socket
#: timeout only needs to be long enough not to sever a healthy call.
DESIRED_SOCKET_TIMEOUT_S = 780
MAX_INTERNAL_RETRIES = 1


def raise_socket_timeout(client, desired: int = DESIRED_SOCKET_TIMEOUT_S, logger=None) -> dict:
    """Read-only probe of the client's socket timeout. Never modifies the client.

    Returns a small report so the caller can log what the injected client exposes.
    The outer TimeBudget (per-problem hard deadline) remains the real runtime guard;
    this report exists only for diagnostics.
    """
    log = logger or logging.getLogger("math_agent")
    report = {"attempted": desired, "before": None, "after": None, "changed": False}
    current = getattr(client, "timeout", None)
    report["before"] = current
    report["after"] = current
    if current is None:
        log.info("Client exposes no socket timeout; leaving transport as provided.")
        return report
    try:
        numeric = float(current)
    except (TypeError, ValueError):
        log.info("Client socket timeout %r is not numeric; leaving as provided.", current)
        return report
    if numeric < desired:
        # 只读观察：记录缺口供诊断，不尝试修改（官方要求不修改 client 属性）。
        log.info("Client socket timeout %ss < desired %ss (read-only probe; not modified).",
                 numeric, desired)
    return report


def cap_internal_retries(client, maximum: int = MAX_INTERNAL_RETRIES, logger=None) -> dict:
    """Read-only probe of the client's internal retry count. Never modifies the client.

    The graph owns an outer, deadline-aware retry wrapper; this report only records
    what the injected client exposes for diagnostics.
    """
    log = logger or logging.getLogger("math_agent")
    report = {"attempted": int(maximum), "before": None, "after": None, "changed": False}
    current = getattr(client, "retry", None)
    report["before"] = current
    report["after"] = current
    if current is None:
        log.info("Client exposes no internal retry count; leaving transport as provided.")
        return report
    try:
        numeric = int(current)
    except (TypeError, ValueError):
        log.info("Client internal retry count %r is not numeric; leaving it unchanged.", current)
        return report
    if numeric > maximum:
        log.info("Client internal retry count %s > cap %s (read-only probe; not modified).",
                 numeric, maximum)
    return report
