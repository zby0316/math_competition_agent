"""全卷完成率引擎（PaperPacer）—— V2 M1。

官方约束（2026-08 判分规则 + 2026-08-12 官方评测实证）：
  - 112 题、并发 3、智能体总运行 6h 封顶，超出后未答题不计分；
  - 官方评测实证（eval_log_c0a00cb…）：V1 每题 1200s 全预算导致 11h19m
    远超 6h、accuracy 0.1429（16/112）——"每题都要完美"在超难题 + 6h 硬
    约束下必然失败。

设计：题间预算池。每道题不再固定拿 full budget，而是按"已用全卷时间 ÷
剩余题数"动态计算该题可花的软预算上限，保证：
  1. 全卷 6h 内必然跑完 112 题（完成率 100%，不出现"超时未答"0 分题）；
  2. 时间自然向"需要深思的题"倾斜（前面的题省下的时间滚动进池子）；
  3. 任何一道题都不会因为前面耗太快而突然没钱（下限保护，仍 ≥ 保底）。

配合 TimeBudget.apply_difficulty_profile：PaperPacer 给出"全卷视角"的
预算帽（paper_cap），难度画像给出"题型视角"的预算帽（difficulty_cap），
两者取 min 作为该题 soft_total——既保证全卷完成，又保留难度差异。

安全边界：
  - 只收紧软预算（可选阶段购买力），不动 remaining_hard（平台 1200s 硬限）；
  - 进行中的调用不被截断；reserve 配额通道（压缩重试/仲裁/应急直答）照常；
  - 每道题仍有保底 soft_total（default 300s），不会出现"简单题 10s 就交卷"。
"""

from __future__ import annotations

import time
from threading import Lock

from config import CONFIG


class PaperPacer:
    """全卷 112 题的节奏控制器：题间预算池 + 动态预算帽。

    用法（在 user_agent.solve 入口处，每题创建 TimeBudget 之前）：
        pacer = PaperPacer.get_instance()          # 单例，全卷共享
        cap = pacer.budget_for(problem_id)         # 得到该题软预算帽（秒）
        budget = TimeBudget(total_seconds=1200)
        budget.soft_total = min(budget.soft_total, cap)
        budget.paper_cap = cap                     # 留痕，便于 trace
        pacer.mark_started(problem_id)

    全卷统计（完成率可视化）：
        pacer.snapshot() -> {total_planned, done, remaining, elapsed, pace, ...}
    """

    _instance: "PaperPacer | None" = None
    _lock = Lock()

    #: 每道题的最低软预算（秒）——低于此值宁可少验证也不能仓促交卷。
    #: 注意：全卷均摊 21600/112 ≈ 193s/题，保底若 ≥193 则全卷必然超时，
    #: 因此保底必须明显小于均摊（120s），真正吃紧时仍保证"每题能答"。
    MIN_SOFT = 120.0
    #: 健康时的软预算帽（秒）：设为平台硬限（1200s）的宽松值——
    #: 实际生效预算由难度画像主导（easy 480 / medium 840 / hard 1200，
    #: apply_difficulty_profile 取 min），PaperPacer 只在"落后"时收紧。
    #: 若这里设得过小（如 300），会压掉 easy 题应有的 480s 并破坏
    #: 既有测试（test_integration_graph 断言 easy→soft_total=480）。
    IDEAL = 1200.0

    def __init__(self, total_seconds: float | None = None,
                 planned: int | None = None) -> None:
        self.total = float(
            total_seconds if total_seconds is not None
            else CONFIG.get("paper_total_seconds", 6 * 3600))
        self.planned = int(planned if planned is not None else 112)
        self._start = time.monotonic()
        self._done = 0
        self._started: set[int] = set()

    # ---- singleton ----

    @classmethod
    def get_instance(cls) -> "PaperPacer":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """每卷评测开始时调用一次（单例无跨题残留的官方红线）"""
        with cls._lock:
            cls._instance = cls()

    # ---- public API ----

    def budget_for(self, problem_id: int) -> float:
        """返回该题的建议软预算帽（秒）。

        判定"落后"用实际速度预测：若按已完成题的平均速度（elapsed/done）
        预测总耗时超过全卷上限的 1.15 倍 → 落后 → 按剩余均摊收紧
        （仍 ≥ MIN_SOFT 保底）；否则健康 → 给足理想预算（IDEAL）。
        首题（done=0）无速度样本 → 健康给足。
        """
        with self._lock:
            elapsed = time.monotonic() - self._start
            remaining_time = max(0.0, self.total - elapsed)
            remaining_problems = max(1, self.planned - self._done)
            if self._done > 0 and elapsed > 0:
                pace = elapsed / self._done  # 实际速度（s/题）
                predicted_total = pace * self.planned
                lagging = predicted_total > self.total * 1.15
            else:
                lagging = False
            # 硬条件：即使按历史速度预测不超，剩余时间不足（均摊 < MIN_SOFT）
            # 也必须收紧——否则最后几题必然超时交白卷。
            per_problem = remaining_time / remaining_problems
            if per_problem < self.MIN_SOFT:
                lagging = True
            if not lagging:
                # 健康：给足理想预算（供难题深思考）
                cap = float(self.IDEAL)
            else:
                cap = max(self.MIN_SOFT, per_problem)
            return cap

    def mark_started(self, problem_id: int) -> None:
        with self._lock:
            self._started.add(problem_id)

    def mark_done(self) -> None:
        with self._lock:
            self._done += 1

    def remaining_planned(self) -> int:
        with self._lock:
            return max(0, self.planned - self._done)

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def pace_ok(self) -> bool:
        """当前节奏是否安全（能在 6h 内完成全部题）。"""
        with self._lock:
            elapsed = time.monotonic() - self._start
            remaining_time = max(0.0, self.total - elapsed)
            remaining_problems = max(1, self.planned - self._done)
            per_problem = remaining_time / remaining_problems
            return per_problem >= self.MIN_SOFT

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = time.monotonic() - self._start
            remaining_time = max(0.0, self.total - elapsed)
            remaining_problems = max(1, self.planned - self._done)
            per_problem = remaining_time / remaining_problems
            return {
                "total_planned": self.planned,
                "done": self._done,
                "remaining_problems": remaining_problems,
                "elapsed_s": round(elapsed, 1),
                "total_s": self.total,
                "remaining_s": round(remaining_time, 1),
                "pace_s_per_problem": round(per_problem, 1),
                "pace_ok": per_problem >= self.MIN_SOFT,
            }
