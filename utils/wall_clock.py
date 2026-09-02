"""单题墙钟预算——竞赛的硬约束是时间，不是 token 数。

为什么需要它（曾对 intern-s2-preview-397b 实测）：

`intern-s2-preview-397b` 是推理模型，返回独立的 `reasoning_content` 字段，
且它与 `content` 一起计入 `max_tokens`。生成速度约 55 token/s，只取决于实际
生成的 token 数——调大 `max_tokens` 不会让一次调用变快，只会把它截断
（CoT 中途被截，部分草稿混进 `content`）。所以 token 上限约束不了单题耗时，
只有时钟能。

曾出现单题 1307s 超时（上限 1200s），但按各阶段耗时之和（约 250s）解释不通。
缺口来自嵌套传输重试：`LLMRetryGuard(max_retries=3)` 包住一个自身按 120s
套接字超时重试 3 次的客户端，最坏情况下单个节点发出 9 个请求、约 1080s。
按次数约束重试解决不了，因为次数被拆到两层、无法统一控制（客户端由平台
注入）。按截止时间约束才行。

因此每个 LLM 调用点都先询问本预算再决定是否花费时间；可选阶段一旦到达
safeguard 就降级为确定性兜底。safeguard 保证始终能输出真实答案，而不是
超时后一无所获。
"""

from __future__ import annotations

import time
from threading import Lock

from config import CONFIG


class TimeBudget:
    """一次 solve() 内所有节点共享的单调单题截止时间。

    三个地平线：
      * ``soft``（total - safeguard）——*可选*工作的截止时间。超过它，图
        不再为仲裁/协调的 LLM 调用付钱，回退到确定性选答案。
      * ``fast_path`` 阈值——还够不够一次完整 LLM 往返？低于它，跳过重试
        和二阶段机会。
      * ``total``——平台的单题硬上限。绝不该主动触及；safeguard 保证我们
        始终离它有一段安全距离。
    """

    def __init__(
        self,
        total_seconds: float | None = None,
        reserve_seconds: float | None = None,
        fast_path_threshold: float | None = None,
        clock=time.monotonic,
    ) -> None:
        self.total = float(
            total_seconds if total_seconds is not None else CONFIG["problem_time_budget_s"]
        )
        self.safeguard = float(
            reserve_seconds if reserve_seconds is not None else CONFIG["time_safeguard_s"]
        )
        self.fast_path_threshold = float(
            fast_path_threshold
            if fast_path_threshold is not None
            else CONFIG["time_fast_path_threshold_s"]
        )
        self._clock = clock
        self._start = clock()
        self._lock = Lock()
        self._spend_log: list[dict] = []

    # ---- 地平线 ----

    def now(self) -> float:
        """读取本预算的时钟。

        调用方要测某次调用耗时，必须用*这个*时钟，而不是直接 `time.monotonic()`：
        两者若不一致，测出的耗时无法与 `remaining()` 比较，所有基于它的截止
        检查都会静默失效。
        """
        return self._clock()

    def elapsed(self) -> float:
        return self._clock() - self._start

    def remaining(self) -> float:
        """可选工作必须停止前还剩的秒数。可能为负。"""
        return self.total - self.safeguard - self.elapsed()

    def remaining_hard(self) -> float:
        """距平台硬上限还剩的秒数。"""
        return self.total - self.elapsed()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def can_afford(self, seconds: float) -> bool:
        return self.remaining() >= float(seconds)

    def fast_path(self) -> bool:
        """当已容不下一次完整额外 LLM 往返时为 True。"""
        return self.remaining() < self.fast_path_threshold

    def timeout_for(self, ceiling: float | None) -> float | None:
        """把节点配置的 ceiling 压到截止时间真正允许的范围。

        节点绝不能活过单题时限：宽松的节点 ceiling 用于接住真正的挂起，而
        截止时间约束真实世界。返回大于等于 1 的小地板而非 0，让已经迟到的
        节点仍有机会把手头答案返回，而不是抛异常。
        """
        allowed = max(1.0, self.remaining_hard())
        if ceiling is None:
            return allowed
        return min(float(ceiling), allowed)

    # ---- 可观测性 ----

    def record(self, label: str, seconds: float) -> None:
        with self._lock:
            self._spend_log.append({"label": label, "seconds": round(float(seconds), 2)})

    def spend_log(self) -> list[dict]:
        with self._lock:
            return list(self._spend_log)

    def snapshot(self) -> dict:
        return {
            "elapsed_s": round(self.elapsed(), 2),
            "remaining_s": round(self.remaining(), 2),
            "total_s": self.total,
            "reserve_s": self.safeguard,
            "fast_path": self.fast_path(),
        }
