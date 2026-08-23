# -*- coding: utf-8 -*-
"""V2.5.7 PV 触发逻辑集成测试（critic_node 内）。

验证：
1. hard 题触发 PV，medium/easy 不触发（adaptive budget）
2. playoff 冲突触发，uncertain_arbitrating 不触发（2026-08-23 干跑实证收紧）
3. PV=flawed 且当前 pass → 升 calc_error（追加 calc_checks）
4. PV 不可用/失败 → 不阻塞，保持原判定
"""
import unittest
from unittest.mock import MagicMock, patch

# 注意：不在模块级修改 sys.path——会污染后续测试（discover 顺序下
# fill_verify 等集成测试依赖干净 sys.path；2026-08-23 实证 test_arbiter_pv
# 的 sys.path.insert(0, ".") 曾破坏 test_integration_fill_verify）。
from nodes.critic_node import critic_node  # noqa: E402


def _state(overrides=None):
    state = {
        "problem": "求 x^2=4 的正根。",
        "question_mode": "computation",
        "difficulty": "medium",
        "validation_status": "match",
        "critic_rounds": 0,
        "critic_trace": [],
        "reasoning_result": {
            "analysis": "a",
            "steps": [{"step_num": 1, "description": "x^2=4 的解"}],
            "answer": "x=2",
        },
        "validated_answer": "x=2",
        "python_output": {},
        "python_evidence_summary": "",
    }
    if overrides:
        state.update(overrides)
    return state


def _deps(pv_result=None, llm_verdict=None):
    deps = MagicMock()
    deps.time_budget = MagicMock()
    deps.time_budget.remaining_hard.return_value = 1000
    deps.time_budget.remaining.return_value = 900
    deps.time_budget.can_afford.return_value = True
    deps.time_budget.expired.return_value = False
    deps.time_budget.llm_calls_exhausted.return_value = False
    deps.token_budget = MagicMock()
    deps.logger = MagicMock()
    deps.client = MagicMock()
    deps.client.chat = MagicMock(return_value='{"complete": true, "missing": [],'
                                              ' "calc_checks": [], "verdict": "pass"}')
    return deps


class TestCriticPVTrigger(unittest.TestCase):
    # run_process_verifier 在 critic_node 函数体内 import（延迟导入），
    # 必须 patch 模块源 utils.process_verifier.run_process_verifier。
    PV = "utils.process_verifier.run_process_verifier"

    @patch("utils.process_verifier.run_process_verifier")
    def test_hard_triggers_pv(self, mock_pv):
        mock_pv.return_value = {"overall": "sound", "flawed_steps": [], "samples": 2}
        deps = _deps()
        state = _state({"difficulty": "hard"})
        with patch("nodes.critic_node.get_deps", return_value=deps):
            out = critic_node(state, None)
        mock_pv.assert_called_once()
        self.assertEqual(out["critic_status"], "pass")
        self.assertEqual(out["next_node"], "coordinator")
        # trace 记录 pv 结果
        self.assertEqual(out["critic_trace"][-1]["pv"]["overall"], "sound")

    @patch("utils.process_verifier.run_process_verifier")
    def test_easy_match_does_not_trigger(self, mock_pv):
        deps = _deps()
        state = _state({"difficulty": "easy", "validation_status": "match"})
        with patch("nodes.critic_node.get_deps", return_value=deps):
            out = critic_node(state, None)
        mock_pv.assert_not_called()
        self.assertEqual(out["critic_status"], "pass")

    @patch("utils.process_verifier.run_process_verifier")
    def test_uncertain_does_not_trigger(self, mock_pv):
        """uncertain_arbitrating 是 V2.5 常态（~60% 题），触发会让 PV 每题必跑。"""
        deps = _deps()
        state = _state({"difficulty": "easy",
                        "validation_status": "uncertain_arbitrating"})
        with patch("nodes.critic_node.get_deps", return_value=deps):
            critic_node(state, None)
        mock_pv.assert_not_called()

    @patch("utils.process_verifier.run_process_verifier")
    def test_playoff_conflict_triggers(self, mock_pv):
        mock_pv.return_value = {"overall": "sound", "flawed_steps": [], "samples": 2}
        deps = _deps()
        state = _state({"difficulty": "easy",
                        "validation_status": "contradict_playoff"})
        with patch("nodes.critic_node.get_deps", return_value=deps):
            critic_node(state, None)
        mock_pv.assert_called_once()

    @patch("utils.process_verifier.run_process_verifier")
    def test_pv_flawed_escalates_to_calc_error(self, mock_pv):
        """PV 发现 critical 漏洞且 critic 原判定 pass → 升 calc_error 触发修复。"""
        mock_pv.return_value = {
            "overall": "flawed",
            "flawed_steps": [{"step": 2, "flaw": "漏了正根"}],
            "samples": 2,
        }
        deps = _deps()
        state = _state({"difficulty": "hard",
                        "reasoning_result": {
                            "analysis": "a",
                            "steps": [{"step_num": 1, "description": "s1"},
                                      {"step_num": 2, "description": "s2"}],
                            "answer": "x=2",
                        }})
        with patch("nodes.critic_node.get_deps", return_value=deps):
            out = critic_node(state, None)
        self.assertEqual(out["critic_status"], "calc_error")
        self.assertEqual(out["next_node"], "reconciliation")
        # calc_checks 包含 PV 漏洞
        checks = out["critic_trace"][-1]["calc_checks"]
        self.assertTrue(any("逻辑漏洞" in str(c.get("check", "")) for c in checks))

    @patch("utils.process_verifier.run_process_verifier")
    def test_pv_failure_does_not_block(self, mock_pv):
        """PV 采样全失败 → unverifiable，不阻塞主链路。"""
        mock_pv.return_value = {"overall": "unverifiable", "flawed_steps": [],
                                "samples": 0, "reason": "全部采样失败"}
        deps = _deps()
        state = _state({"difficulty": "hard"})
        with patch("nodes.critic_node.get_deps", return_value=deps):
            out = critic_node(state, None)
        self.assertEqual(out["critic_status"], "pass")
        self.assertEqual(out["next_node"], "coordinator")


if __name__ == "__main__":
    unittest.main()
