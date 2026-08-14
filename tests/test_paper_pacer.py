# -*- coding: utf-8 -*-
"""V2 M1 PaperPacer 全卷完成率引擎测试。

覆盖：健康给足 / 落后收紧 / 保底守住 / 首题默认 / 全卷节奏统计。
"""
import unittest

import utils.paper_pacer as pp
from utils.paper_pacer import PaperPacer


class _FakeClock:
    def __init__(self, start: float = 10000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t


class TestPaperPacer(unittest.TestCase):

    def setUp(self):
        self._orig = pp.time.monotonic
        self.fc = _FakeClock()
        pp.time.monotonic = self.fc
        PaperPacer.reset()
        self.pacer = PaperPacer()

    def tearDown(self):
        pp.time.monotonic = self._orig

    def test_first_problem_gets_ideal_budget(self):
        # 首题（无速度样本）→ 健康，给足 IDEAL（宽松值，难度画像主导）
        self.fc.t = 10000.0
        cap = self.pacer.budget_for(0)
        self.assertAlmostEqual(cap, PaperPacer.IDEAL, delta=1.0)

    def test_healthy_pace_gets_ideal_budget(self):
        # 50 题 1h → pace 72s/题，预测 8064s << 21600 → 健康 IDEAL
        self.pacer._done = 50
        self.fc.t = 10000.0 + 3600
        cap = self.pacer.budget_for(50)
        self.assertAlmostEqual(cap, PaperPacer.IDEAL, delta=1.0)

    def test_lagging_pace_tightens_but_floor_holds(self):
        # 20 题 2h → pace 360s/题，预测 40320 > 18000×1.15 → 落后
        # 剩余 10800s / 92 题 = 117.4s < MIN_SOFT(120) → 保底 120
        self.pacer._done = 20
        self.fc.t = 10000.0 + 7200
        cap = self.pacer.budget_for(20)
        self.assertAlmostEqual(cap, PaperPacer.MIN_SOFT, delta=1.0)
        self.assertGreaterEqual(cap, PaperPacer.MIN_SOFT)

    def test_extreme_lag_floor_holds(self):
        # 110 题 5.95h → 剩余 180s / 2 题 = 90s → 保底 120
        self.pacer._done = 110
        self.fc.t = 10000.0 + 21420
        cap = self.pacer.budget_for(110)
        self.assertAlmostEqual(cap, PaperPacer.MIN_SOFT, delta=1.0)

    def test_mark_done_drives_pace(self):
        self.pacer.mark_done()
        self.assertEqual(self.pacer._done, 1)
        snap = self.pacer.snapshot()
        self.assertEqual(snap["done"], 1)
        self.assertEqual(snap["total_planned"], 112)
        self.assertIn("pace_s_per_problem", snap)

    def test_singleton_reset(self):
        PaperPacer.reset()
        a = PaperPacer.get_instance()
        PaperPacer.reset()
        b = PaperPacer.get_instance()
        self.assertIsNot(a, b)


if __name__ == "__main__":
    unittest.main()
