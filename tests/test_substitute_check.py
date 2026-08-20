# -*- coding: utf-8 -*-
"""V2.6.1 单候选代回核验测试（substitute 节点 + cross_validator 路由）。

覆盖：PASS 放行 critic / FAIL 定向修复重算 / 无代码良性放行 / 预算不足跳过 /
match 路由（medium/hard 计算题进核验、easy 直通）/ 总开关。
"""
import unittest

from config import CONFIG
from nodes.cross_validator_node import cross_validator_node
from nodes.substitute_node import substitute_node
from utils.deps import Deps, MockClient
from utils.python_mcp_client import PythonMCPClient
from utils.time_budget import TimeBudget


class _Cfg:
    """最小 config：注入 deps。"""

    def __init__(self, deps):
        self._c = {"configurable": {"deps": deps}}

    def get(self, k, default=None):
        return self._c.get(k, default)

    def __getitem__(self, k):
        return self._c[k]


def _pass_code():
    return ('```python\n'
            'print("核验结果:", "PASS")\n'
            'print("核验说明:", "代回验证成立")\n```')


def _fail_code():
    return ('```python\n'
            'print("核验结果:", "FAIL")\n'
            'print("核验说明:", "残差非零")\n```')


def _node_state(answer="5"):
    return {"problem": "计算 2+3 的值。", "validated_answer": answer,
            "difficulty": "hard", "question_mode": "computation"}


def _match_state(difficulty="hard"):
    return {
        "problem": "计算 2+3 的值。",
        "question_mode": "computation",
        "category": "数学分析",
        "difficulty": difficulty,
        "reasoning_result": {"answer": "5",
                             "steps": [{"step_num": 1, "description": "直接计算"}]},
        "python_code": "print('最终答案:', 2+3)",
        "python_output": {
            "success": True, "answer": "5",
            "stdout": "验证状态: PASS\n最终答案: 5",
            "evidence_status": "support", "evidence_summary": "一致",
            "contradictions": [],
        },
        "reconciliation_round": 0,
    }


class TestSubstituteNode(unittest.TestCase):

    def _deps(self, responses, reserve=0):
        return Deps(client=MockClient(responses), skills_loader=None,
                    mcp_client=PythonMCPClient(), token_budget=None,
                    time_budget=TimeBudget(total_seconds=1200, reserve_seconds=reserve))

    def test_pass_goes_to_critic(self):
        out = substitute_node(_node_state(), _Cfg(self._deps([_pass_code()])))
        self.assertEqual(out["substitution_status"], "pass")
        self.assertEqual(out["next_node"], "critic")

    def test_fail_routes_to_reconciliation_with_hint(self):
        out = substitute_node(_node_state(), _Cfg(self._deps([_fail_code()])))
        self.assertEqual(out["substitution_status"], "fail")
        self.assertEqual(out["next_node"], "reconciliation")
        self.assertEqual(out["repair_hint_source"], "substitution")
        self.assertTrue(out["reasoning_retry_hint"])
        self.assertTrue(out["python_retry_hint"])

    def test_no_code_is_benign(self):
        # 无代码块 → 无法机械核验 → 良性放行 critic（不阻塞出答案）
        out = substitute_node(_node_state(), _Cfg(self._deps(["没有代码，直接结论"])))
        self.assertEqual(out["substitution_status"], "indecisive")
        self.assertEqual(out["next_node"], "critic")

    def test_unaffordable_skips(self):
        # 硬时间不足 → 跳过（不花钱）
        deps = self._deps([], reserve=0)
        deps.time_budget = TimeBudget(total_seconds=200, reserve_seconds=0)
        out = substitute_node(_node_state(), _Cfg(deps))
        self.assertEqual(out["substitution_status"], "skipped")
        self.assertEqual(out["next_node"], "critic")

    def test_disable_flag_skips(self):
        saved = CONFIG.get("enable_substitution_check")
        CONFIG["enable_substitution_check"] = False
        try:
            out = substitute_node(_node_state(), _Cfg(self._deps([])))
            self.assertEqual(out["substitution_status"], "skipped")
            self.assertEqual(out["next_node"], "critic")
        finally:
            CONFIG["enable_substitution_check"] = saved


class TestMatchRoutesToSubstitute(unittest.TestCase):

    def test_hard_match_routes_to_substitute(self):
        out = cross_validator_node(_match_state("hard"), _Cfg(
            Deps(client=MockClient([]), skills_loader=None, mcp_client=None,
                 token_budget=None, time_budget=None)))
        self.assertEqual(out["next_node"], "substitute")
        self.assertIn("substitution_check", out["validation_details"]["routing_reason"])

    def test_medium_match_routes_to_substitute(self):
        out = cross_validator_node(_match_state("medium"), _Cfg(
            Deps(client=MockClient([]), skills_loader=None, mcp_client=None,
                 token_budget=None, time_budget=None)))
        self.assertEqual(out["next_node"], "substitute")

    def test_easy_match_stays_on_fast_path(self):
        # easy 保持直通（不花核验预算）
        out = cross_validator_node(_match_state("easy"), _Cfg(
            Deps(client=MockClient([]), skills_loader=None, mcp_client=None,
                 token_budget=None, time_budget=None)))
        self.assertEqual(out["next_node"], "coordinator")

    def test_already_checked_does_not_reroute(self):
        # 已核验过（substitution_trace 非空）→ 不再重入
        state = _match_state("hard")
        state["substitution_trace"] = [{"status": "executed", "verdict": "PASS"}]
        out = cross_validator_node(state, _Cfg(
            Deps(client=MockClient([]), skills_loader=None, mcp_client=None,
                 token_budget=None, time_budget=None)))
        self.assertEqual(out["next_node"], "coordinator")


if __name__ == "__main__":
    unittest.main()
