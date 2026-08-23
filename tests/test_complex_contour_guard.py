# -*- coding: utf-8 -*-
"""V2.5.7 复参数化积分守卫测试（python_agent_node prompt 注入）。"""
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")

from nodes.python_agent_node import python_agent_node  # noqa: E402


def _state(problem):
    return {
        "problem": problem,
        "category": "复分析",
        "question_mode": "computation",
        "reasoning_result": {"answer": "2*pi*I", "analysis": "a",
                             "steps": [{"step_num": 1, "description": "s"}]},
        "branch_hint": "",
    }


class TestComplexContourGuard(unittest.TestCase):
    def _run(self, problem):
        deps = MagicMock()
        deps.skills_loader = MagicMock()
        deps.skills_loader.get_validation_script.return_value = ""
        deps.skills_loader.get_skill_document.return_value = ""
        deps.client = MagicMock()
        deps.client.chat = MagicMock(
            return_value="import sympy as sp\nprint(2*sp.pi*sp.I)")
        deps.mcp_client = MagicMock()
        deps.token_budget = MagicMock()
        deps.token_budget.is_tight.return_value = False
        deps.time_budget = MagicMock()
        deps.time_budget.remaining_hard.return_value = 1000
        deps.time_budget.remaining.return_value = 900
        deps.time_budget.can_afford.return_value = True
        deps.time_budget.expired.return_value = False
        deps.time_budget.llm_calls_exhausted.return_value = False
        deps.logger = MagicMock()
        with patch("nodes.python_agent_node.get_deps", return_value=deps):
            python_agent_node(_state(problem), None)
        # 收集发给模型的 prompt 文本
        prompts = []
        for call in deps.client.chat.call_args_list:
            if call.kwargs:
                msgs = call.kwargs.get("messages") or []
            else:
                msgs = call.args[0] if call.args else []
            for m in msgs:
                if isinstance(m, dict):
                    prompts.append(str(m.get("content") or ""))
        return "\n".join(prompts)

    def test_contour_problem_injects_guard(self):
        prompt = self._run("用柯西积分公式计算 $\\oint_{|z|=1}\\frac{e^{z}}{z}\\,dz$。")
        self.assertIn("复积分数值对照强制条款", prompt)
        self.assertIn("错误返回 0", prompt)

    def test_plain_problem_no_guard(self):
        prompt = self._run("求 $\\int_0^1 x^2\\,dx$。")
        self.assertNotIn("复积分数值对照强制条款", prompt)

    def test_residue_keyword_injects_guard(self):
        prompt = self._run("用留数定理计算 $\\oint_{|z|=2}\\frac{z}{z^2+1}\\,dz$。")
        self.assertIn("复积分数值对照强制条款", prompt)


if __name__ == "__main__":
    unittest.main()
