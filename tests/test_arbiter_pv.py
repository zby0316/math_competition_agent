# -*- coding: utf-8 -*-
"""V2.5.7 仲裁前 PV 第三路证据测试（semantic_arbiter_node）。

验证：
1. uncertain_arbitrating 场景触发 PV 并注入 prompt（sound 证据块）
2. PV=flawed 时注入漏洞证据块
3. 非 uncertain 场景不触发（match/playoff 不重复）
4. PV 失败不阻塞仲裁（无证据块，照常仲裁）
"""
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")

from nodes.semantic_arbiter_node import semantic_arbiter_node  # noqa: E402


def _state(validation_status="uncertain_arbitrating"):
    return {
        "problem": "求 x^2=4 的正根。",
        "question_mode": "computation",
        "validation_status": validation_status,
        "validation_details": {"reason": "语义判定无法确定", "problem_type": "computation"},
        "reasoning_result": {
            "analysis": "a",
            "steps": [{"step_num": 1, "description": "x^2=4 → x=±2"},
                      {"step_num": 2, "description": "取正根 x=2"}],
            "answer": "x=2",
        },
        "python_output": {"success": True, "answer": "-2",
                          "stdout": "计算得 x=-2", "evidence_status": "support"},
        "python_code": "print(-2)",
        "semantic_arbiter_attempts": 0,
        "semantic_arbiter_trace": [],
    }


def _deps(pv_result=None):
    deps = MagicMock()
    deps.token_budget = MagicMock()
    deps.token_budget.can_afford.return_value = True
    deps.time_budget = MagicMock()
    deps.time_budget.remaining_hard.return_value = 1000
    deps.time_budget.remaining.return_value = 900
    deps.time_budget.can_afford.return_value = True
    deps.time_budget.expired.return_value = False
    deps.time_budget.llm_calls_exhausted.return_value = False
    deps.logger = MagicMock()
    deps.client = MagicMock()
    # 仲裁预填返回：选择 reasoning 候选（C_ 开头随机 id 无法预知，用 ABSTAIN
    # 无法测选边——这里让仲裁输出选择含 "x=2" 的候选需要动态 id。
    # 简化：让仲裁 abstain 返回，仅验证 prompt 是否含 PV 证据块。
    deps.client.chat = MagicMock(return_value="ABSTAIN")
    return deps


class TestArbiterPV(unittest.TestCase):
    @patch("utils.process_verifier.run_process_verifier")
    def test_uncertain_triggers_pv_sound(self, mock_pv):
        mock_pv.return_value = {"overall": "sound", "flawed_steps": [], "samples": 1}
        deps = _deps()
        state = _state()
        with patch("nodes.semantic_arbiter_node.get_deps", return_value=deps):
            semantic_arbiter_node(state, None)
        mock_pv.assert_called_once()
        # 检查发给 client 的 prompt 含 PV 证据块
        prompts = []
        for call in deps.client.chat.call_args_list:
            for m in (call.kwargs.get("messages") or call.args[0] or []):
                if isinstance(m, dict):
                    prompts.append(str(m.get("content") or ""))
        joined = "\n".join(prompts)
        self.assertIn("PROCESS VERIFICATION", joined)
        self.assertIn("sound", joined)

    @patch("utils.process_verifier.run_process_verifier")
    def test_pv_flawed_injects_vulnerability(self, mock_pv):
        mock_pv.return_value = {"overall": "flawed", "samples": 1,
                                "flawed_steps": [{"step": 2, "flaw": "漏了正根"}]}
        deps = _deps()
        state = _state()
        with patch("nodes.semantic_arbiter_node.get_deps", return_value=deps):
            semantic_arbiter_node(state, None)
        prompts = []
        for call in deps.client.chat.call_args_list:
            for m in (call.kwargs.get("messages") or call.args[0] or []):
                if isinstance(m, dict):
                    prompts.append(str(m.get("content") or ""))
        joined = "\n".join(prompts)
        self.assertIn("关键逻辑漏洞", joined)
        self.assertIn("漏了正根", joined)

    @patch("utils.process_verifier.run_process_verifier")
    def test_match_does_not_trigger(self, mock_pv):
        deps = _deps()
        state = _state("match")
        with patch("nodes.semantic_arbiter_node.get_deps", return_value=deps):
            semantic_arbiter_node(state, None)
        mock_pv.assert_not_called()

    @patch("utils.process_verifier.run_process_verifier")
    def test_pv_failure_does_not_block(self, mock_pv):
        mock_pv.return_value = {"overall": "unverifiable", "flawed_steps": [],
                                "samples": 0, "reason": "全部采样失败"}
        deps = _deps()
        state = _state()
        with patch("nodes.semantic_arbiter_node.get_deps", return_value=deps):
            out = semantic_arbiter_node(state, None)
        # 不阻塞：仲裁正常返回（abstain 或 fallback）
        self.assertIn(out.get("semantic_arbiter_status", ""),
                      ("abstained", "skipped", "selected", "invalid_format", "budget_exhausted"))


if __name__ == "__main__":
    unittest.main()
