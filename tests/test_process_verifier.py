# -*- coding: utf-8 -*-
"""V2.5.7 过程验证器 PV 单元测试。

覆盖：
1. _parse_verdict：prefill JSON / 容错提取 / 非法输入
2. run_process_verifier：多数票聚合（2/3 采样判某步无效 → flawed）
3. 触发条件：hard/冲突触发，medium 快速路径不触发（经 critic_node 逻辑）
"""
import sys
import unittest
from unittest.mock import MagicMock

# 注意：不在模块级修改 sys.path——会污染后续测试（test_platform_defenses
# 的 harshest 用例依赖干净的 sys.path 才能验证 fallback 自举）。
from utils.process_verifier import _parse_verdict, run_process_verifier  # noqa: E402


class TestParseVerdict(unittest.TestCase):
    def test_prefill_json(self):
        raw = '{"step_verdicts": [{"step": 1, "valid": true, "severity": "minor", "flaw": ""},' \
              '{"step": 2, "valid": false, "severity": "critical", "flaw": "漏了正根"}],' \
              ' "overall": "flawed"}'
        out = _parse_verdict(raw)
        self.assertIsNotNone(out)
        self.assertEqual(out["overall"], "flawed")
        self.assertEqual(len(out["step_verdicts"]), 2)
        self.assertTrue(out["step_verdicts"][0]["valid"])
        self.assertFalse(out["step_verdicts"][1]["valid"])
        self.assertEqual(out["step_verdicts"][1]["severity"], "critical")
        self.assertEqual(out["step_verdicts"][1]["flaw"], "漏了正根")

    def test_minor_only_is_sound(self):
        # 全部步骤仅 minor 表述问题 → overall 应为 sound（不升级）
        raw = '{"step_verdicts": [{"step": 1, "valid": false, "severity": "minor",' \
              '"flaw": "表述不清"}], "overall": "flawed"}'
        out = _parse_verdict(raw)
        self.assertIsNotNone(out)
        self.assertEqual(out["overall"], "sound")
        self.assertEqual(out["step_verdicts"][0]["severity"], "minor")

    def test_noisy_text_extraction(self):
        raw = ("思考过程……\n先看第一步。\n"
               '{"step_verdicts": [{"step": 1, "valid": false, "severity": "critical",'
               ' "flaw": "x"}]}'
               "\n以上。")
        out = _parse_verdict(raw)
        self.assertIsNotNone(out)
        self.assertEqual(out["overall"], "flawed")

    def test_invalid_input(self):
        self.assertIsNone(_parse_verdict(""))
        self.assertIsNone(_parse_verdict("没有 JSON"))
        self.assertIsNone(_parse_verdict('{"step_verdicts": "notalist"}'))


def _make_deps(samples: list[dict | None]):
    deps = MagicMock()
    deps.client = MagicMock()
    deps.time_budget = MagicMock()
    deps.time_budget.remaining_hard.return_value = 1000
    deps.time_budget.remaining.return_value = 900
    deps.time_budget.can_afford.return_value = True
    deps.time_budget.expired.return_value = False
    deps.time_budget.llm_calls_exhausted.return_value = False
    deps.token_budget = MagicMock()
    deps.logger = MagicMock()
    call = MagicMock(side_effect=samples)
    deps.client.chat = call
    return deps


class TestRunProcessVerifier(unittest.TestCase):
    def test_majority_vote_flawed(self):
        # 2 次采样一致判步骤 2 critical 无效 → flawed（n=2 全票门槛）
        samples = [
            '{"step_verdicts": [{"step": 1, "valid": true, "severity": "minor", "flaw": ""},'
            '{"step": 2, "valid": false, "severity": "critical", "flaw": "漏了正根"}],'
            ' "overall": "flawed"}',
            '{"step_verdicts": [{"step": 1, "valid": true, "severity": "minor", "flaw": ""},'
            '{"step": 2, "valid": false, "severity": "critical", "flaw": "计算错误"}],'
            ' "overall": "flawed"}',
        ]
        deps = _make_deps(samples)
        state = {
            "problem": "求 x^2=4 的正根。",
            "reasoning_result": {"analysis": "a", "steps": [
                {"step_num": 1, "description": "x^2=4 → x=±2"},
                {"step_num": 2, "description": "取正根 x=2"}]},
        }
        out = run_process_verifier(deps, state, "x=2")
        self.assertEqual(out["overall"], "flawed")
        self.assertEqual(out["samples"], 2)
        self.assertEqual(out["flawed_steps"][0]["step"], 2)
        self.assertIn("漏了正根", out["flawed_steps"][0]["flaw"])

    def test_minor_does_not_escalate(self):
        # 2 次采样一致判步骤 minor 无效 → sound（不升级，idx=8 干跑教训）
        samples = [
            '{"step_verdicts": [{"step": 1, "valid": false, "severity": "minor",'
            ' "flaw": "表述不清"}], "overall": "sound"}',
            '{"step_verdicts": [{"step": 1, "valid": false, "severity": "minor",'
            ' "flaw": "简写"}], "overall": "sound"}',
        ]
        deps = _make_deps(samples)
        state = {
            "problem": "p",
            "reasoning_result": {"analysis": "a",
                                 "steps": [{"step_num": 1, "description": "s"}]},
        }
        out = run_process_verifier(deps, state, "ans")
        self.assertEqual(out["overall"], "sound")

    def test_majority_vote_sound(self):
        samples = [
            '{"step_verdicts": [{"step": 1, "valid": true, "severity": "minor", "flaw": ""}],'
            ' "overall": "sound"}',
            '{"step_verdicts": [{"step": 1, "valid": false, "severity": "minor",'
            ' "flaw": "x"}], "overall": "sound"}',
        ]
        deps = _make_deps(samples)
        state = {
            "problem": "p",
            "reasoning_result": {"analysis": "a",
                                 "steps": [{"step_num": 1, "description": "s"}]},
        }
        out = run_process_verifier(deps, state, "ans")
        self.assertEqual(out["overall"], "sound")

    def test_all_samples_fail_unverifiable(self):
        deps = _make_deps([None, None])
        state = {"problem": "p",
                 "reasoning_result": {"steps": [{"step_num": 1, "description": "s"}]}}
        out = run_process_verifier(deps, state, "ans")
        self.assertEqual(out["overall"], "unverifiable")
        self.assertEqual(out["samples"], 0)

    def test_no_reasoning_unverifiable(self):
        deps = _make_deps([])
        out = run_process_verifier(deps, {"problem": "p"}, "ans")
        self.assertEqual(out["overall"], "unverifiable")


if __name__ == "__main__":
    unittest.main()
