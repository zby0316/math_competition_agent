"""一次额外的尝试要花多少钱？问时钟，而不是一个乐观常量。

只有当截止时间能负担一次与上次同样耗时的调用时，节点重试才值得买。这套逻辑
要防住的失败（一道奥林匹克级题目上曾出现）：推理与 Python 各跑了两次、每次
510-552s，两个分支的第二次尝试获批时都只剩约 349s 预算，最终题目超时 198s、
什么都没给出。两个分支都花光了全部预算却零交付——最差的交易。
"""

from __future__ import annotations

#: 当本题还没有可比实测值时的兜底估算。取自已测出的难题调用（510-552s）
#: 而非简单题，因为要点就是避免批准一次我们完不成的尝试。
DEFAULT_RETRY_COST_S = 300.0


def last_attempt_cost(time_budget, label_prefix: str) -> float:
    """最近一次标签以 `label_prefix` 开头的调用的耗时。

    当本题还没有可比调用时回退到 DEFAULT_RETRY_COST_S。
    失败的尝试也计入：一次传输故障烧掉 363s，就还会再烧 363s。

    ``*_compressed`` 标签不计入：压缩重试是短输出的熔断挽救（~16s），与完整
    调用的成本不在同一量级。曾实测对账把压缩重试的 16s 误当成"再来一轮很
    便宜"（真实首轮成本 550s），在硬难题上白白重跑一轮 solving 子图、烧掉
    额外 550s 却拿不到答案。
    """
    if time_budget is None:
        return 0.0
    matching = [entry["seconds"] for entry in time_budget.spend_log()
                if str(entry.get("label", "")).startswith(label_prefix)
                and "compressed" not in str(entry.get("label", ""))]
    return matching[-1] if matching else DEFAULT_RETRY_COST_S


def can_afford_retry(time_budget, label_prefix: str) -> bool:
    """本次同类尝试是否还能放进剩余的可选工作窗口。"""
    if time_budget is None:
        return True
    return time_budget.can_afford(last_attempt_cost(time_budget, label_prefix))
