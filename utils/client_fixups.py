"""调高注入客户端的套接字超时，让慢但健康的调用存活。

曾出现的失败：一次运行里每道题的推理尝试都在 363s 死于 `reasoning:failed`——
正好是客户端 120s 套接字超时的 3 倍，即客户端内部重试全部耗尽。那次调用并没有
挂起；该模型单次推理调用单独跑就要 77-116s，四题并行负载下超过 120s 很正常。
所以 120s 套接字超时在这里不是防挂起，而是*保证*难题必失败、什么都不返回。

这个问题在分类从约 40s 降到约 1s 之后才暴露：那段延迟一直在无意中错开并行
题目的启动，去掉它以后四道题同时发出推理调用。修好某个阶段反而暴露了真正的
瓶颈。

范围：仅传输层。平台仍然拥有客户端、模型、限流与 token 记账——这里只够一个
属性、尽力而为，属性缺失或只读时就不动客户端。刻意保守，因为注入的客户端
不是我们的东西，不该重塑。
"""

from __future__ import annotations

import logging

#: 难题的推理调用是图中最长的一次请求。真正约束运行时长的是单题截止时间
#: （TimeBudget），所以套接字超时只需长到不切断健康调用即可。
#:
#: 2026-08-22 官方评测复盘：112 题上 684 次请求里 50+ 次 Read timed out——
#: 780s 读超时在 Omni d≥9.5 / 京都研究生级难题上切断了仍健康的调用（该模型
#: CoT 普遍超过 780s）。首轮 max_tokens 已上调到 30720（典型 ~600s，慢速负载
#: 下 ~900s），读超时须跟到 1200s，贴近单题 20 分钟硬上限，保证任何在软窗口
#: 内发出的调用都能在硬上限前收尾或超时返回，而不再被传输层提前掐断。
DESIRED_SOCKET_TIMEOUT_S = 1200
MAX_INTERNAL_RETRIES = 1


def raise_socket_timeout(client, desired: int = DESIRED_SOCKET_TIMEOUT_S, logger=None) -> dict:
    """尽力把 `client.timeout` 调高。绝不降低，绝不抛异常。

    返回一份小报告，让调用方记录实际发生的情况，而不是假定生效了。
    """
    log = logger or logging.getLogger("math_agent")
    report = {"attempted": desired, "before": None, "after": None, "changed": False}

    current = getattr(client, "timeout", None)
    report["before"] = current
    if current is None:
        log.info("客户端未暴露套接字超时；按平台提供的原样使用传输层。")
        return report
    try:
        numeric = float(current)
    except (TypeError, ValueError):
        log.info("客户端套接字超时 %r 不是数字；按原样保留。", current)
        return report
    if numeric >= desired:
        report["after"] = current
        return report

    try:
        client.timeout = desired
    except Exception as exc:  # noqa: BLE001 - 只读属性或冻结客户端。
        log.warning("无法调高客户端套接字超时（%s）；保留 %ss。", exc, numeric)
        report["after"] = current
        return report

    report["after"] = getattr(client, "timeout", None)
    report["changed"] = report["after"] != current
    if report["changed"]:
        log.info("已把客户端套接字超时从 %ss 调高到 %ss，避免长推理调用在生成"
                 "中途被切断。", numeric, report["after"])
    return report


def cap_internal_retries(client, maximum: int = MAX_INTERNAL_RETRIES, logger=None) -> dict:
    """尽力压低客户端*内部*传输重试循环的次数。

    图拥有外层、感知截止时间的重试包装。若注入的客户端带着 ``retry=3`` 不管，
    会形成乘法重试预算（内部 3 × 外部 3 个请求），在长套接字超时后可能超出
    单题墙钟上限。没有可变 ``retry`` 属性的客户端保持原样并照实报告。
    """
    log = logger or logging.getLogger("math_agent")
    report = {"attempted": int(maximum), "before": None, "after": None, "changed": False}
    current = getattr(client, "retry", None)
    report["before"] = current
    if current is None:
        log.info("客户端未暴露内部重试次数；按平台提供的原样使用传输层。")
        return report
    try:
        numeric = int(current)
    except (TypeError, ValueError):
        log.info("客户端内部重试次数 %r 不是数字；保持原样。", current)
        return report
    target = max(1, int(maximum))
    if numeric <= target:
        report["after"] = current
        return report
    try:
        client.retry = target
    except Exception as exc:  # noqa: BLE001 - 注入的客户端可能是只读的。
        log.warning("无法压低客户端内部重试次数（%s）；保持 %s。", exc, numeric)
        report["after"] = current
        return report
    report["after"] = getattr(client, "retry", None)
    report["changed"] = report["after"] != current
    if report["changed"]:
        log.info("已把客户端内部重试次数从 %s 压到 %s；外层重试仍受截止时间约束。",
                 numeric, report["after"])
    return report
