# -*- coding: utf-8 -*-
"""V2.5 缺口1/2：异构B路径扩展 + 证据闸门堵漏。

缺口1：B 路径触发从"4 领域白名单"扩展为"hard 难度 或 置信<0.85 全领域扇出"。
缺口2：_evidence_override 在 answer_locked=True 时也生效（Python contradict 仍覆盖）。
"""
import unittest

from config import CONFIG


class TestDiverseBranchExpansion(unittest.TestCase):
    """缺口1：B 路径在 hard 难度 / 低置信时全领域触发。"""

    def _want(self, state):
        # 复现 _want_diverse_branch 的核心判定逻辑
        from graph.solving_subgraph import _want_diverse_branch
        return _want_diverse_branch(state, {})

    def test_hard_triggers_any_domain(self):
        """hard 难度：即使非高风险领域也扇出 B 路径。"""
        # 概率论（非高风险领域）+ hard
        state = {"question_mode": "computation", "reconciliation_round": 0,
                 "category_confidence": 0.7, "category": "概率论", "difficulty": "hard"}
        self.assertTrue(self._want(state))

    def test_low_confidence_triggers_any_domain(self):
        """低置信(<0.85)即使非高风险领域也扇出。"""
        state = {"question_mode": "computation", "reconciliation_round": 0,
                 "category_confidence": 0.6, "category": "复分析", "difficulty": "medium"}
        self.assertTrue(self._want(state))

    def test_objective_mode_still_skips(self):
        """客观题仍不扇出（快速路径不变）。"""
        state = {"question_mode": "choice", "reconciliation_round": 0,
                 "category_confidence": 0.5, "category": "离散数学", "difficulty": "hard"}
        self.assertFalse(self._want(state))

    def test_fast_confidence_now_fires(self):
        """V2.9 方法差距修复：fast 档（≥0.90）也扇出 B 路径。

        分类器把科研级题判成 easy+0.98 高置信（实证），fast 档短路会让这些题
        只有单路径 A、答错就错（官方 retry=0 根因）。置信度信号不可靠，异构
        互验不能拿它当门槛——只保留「时间 fast path」这一硬护栏。
        """
        state = {"question_mode": "computation", "reconciliation_round": 0,
                 "category_confidence": 0.95, "category": "复分析", "difficulty": "hard"}
        self.assertTrue(self._want(state))


class TestEvidenceOverrideLocked(unittest.TestCase):
    """缺口2：_evidence_override 在 answer_locked 时也覆盖错误答案。"""

    def test_override_fires_even_when_locked(self):
        """coordinator 中证据闸门不再被 answer_locked 跳过。"""
        import inspect
        import nodes.coordinator_node as mod
        src = inspect.getsource(mod)
        # 关键：证据闸门调用不再包在 `if not answer_locked` 里
        self.assertNotIn("if not state.get(\"answer_locked\"):\n        validated, evidence_note = _evidence_override",
                         src)
        # 闸门调用应是无条件执行
        self.assertIn("validated, evidence_note = _evidence_override(state, validated)", src)

    def test_playoff_decisive_still_exempt(self):
        """playoff decisive 仍豁免（代回复算证据更强）。"""
        from nodes.coordinator_node import _evidence_override
        state = {"playoff_status": "decisive", "python_output": {},
                 "reasoning_result": {}}
        out, note = _evidence_override(state, "some_answer")
        self.assertEqual(out, "some_answer")
        self.assertEqual(note, "")


if __name__ == "__main__":
    unittest.main()
