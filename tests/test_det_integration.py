# -*- coding: utf-8 -*-
"""V2.7-2 det_solver 集成路径测试（推理节点接线）。

det_solver 管线本身由 tests/test_det_solver.py 覆盖（子代理产物）；这里测
_nodes.reasoning_agent_node._det_solve 的接线语义：
- 命中自验结果 → reasoning_result(answer_source=det_solver) 短路 LLM；
- 客观题/多问项不短路；空答案/脏答案不短路；
- det_solver 模块缺失/抛异常 → 静默 None（交回 LLM）。
"""
import unittest

from config import CONFIG
from utils.deps import Deps, MockClient

# 注意：nodes/__init__.py 的 from .reasoning_agent_node import reasoning_agent_node
# 会把 nodes.reasoning_agent_node 绑定为**函数**而非模块，必须用 importlib 取模块。
import importlib
rn = importlib.import_module("nodes.reasoning_agent_node")


def _state(problem, question_mode="computation"):
    return {"problem": problem, "question_mode": question_mode,
            "difficulty": "hard", "reasoning_light": False}


class _FakeDet:
    def __init__(self, result):
        self.result = result

    def __call__(self, problem):
        if self.result is Exception:
            raise RuntimeError("boom")
        return self.result


class TestDetSolveIntegration(unittest.TestCase):
    def setUp(self):
        self._saved_enable = CONFIG.get("enable_det_solver")
        CONFIG["enable_det_solver"] = True
        self._saved_fn = rn._DET_SOLVER

    def tearDown(self):
        CONFIG["enable_det_solver"] = self._saved_enable
        rn._DET_SOLVER = self._saved_fn

    def test_hit_short_circuits_with_det_source(self):
        rn._DET_SOLVER = _FakeDet(
            {"answer": "42", "kind": "test_pipe", "evidence": "自验"})
        out = rn._det_solve(_state("计算 6×7"), Deps(client=MockClient([])))
        self.assertIsNotNone(out)
        self.assertEqual(out["reasoning_result"]["answer_source"], "det_solver")
        self.assertEqual(out["reasoning_result"]["answer"], "42")
        self.assertEqual(out["reasoning_attempts"], 0)
        self.assertIn("test_pipe", out["reasoning_trace"][0]["reason"])

    def test_empty_answer_noop(self):
        rn._DET_SOLVER = _FakeDet({"answer": "", "kind": "x", "evidence": "y"})
        self.assertIsNone(rn._det_solve(_state("计算 6×7"), Deps(client=MockClient([]))))

    def test_noise_answer_noop(self):
        rn._DET_SOLVER = _FakeDet(
            {"answer": "无法确定", "kind": "x", "evidence": "y"})
        out = rn._det_solve(_state("计算 6×7"), Deps(client=MockClient([])))
        self.assertIsNone(out)

    def test_objective_skips(self):
        rn._DET_SOLVER = _FakeDet({"answer": "A", "kind": "x", "evidence": "y"})
        self.assertIsNone(rn._det_solve(
            _state("下列哪个是素数？A.2 B.3", question_mode="choice"),
            Deps(client=MockClient([]))))

    def test_multi_ask_skips(self):
        rn._DET_SOLVER = _FakeDet({"answer": "1", "kind": "x", "evidence": "y"})
        self.assertIsNone(rn._det_solve(
            _state("求 2+3 和 4+5 的值"), Deps(client=MockClient([]))))

    def test_exception_noop(self):
        rn._DET_SOLVER = _FakeDet(Exception)
        self.assertIsNone(rn._det_solve(_state("计算 6×7"), Deps(client=MockClient([]))))

    def test_config_off(self):
        CONFIG["enable_det_solver"] = False
        rn._DET_SOLVER = _FakeDet({"answer": "42", "kind": "x", "evidence": "y"})
        self.assertIsNone(rn._det_solve(_state("计算 6×7"), Deps(client=MockClient([]))))

    def test_module_missing_degrades(self):
        # 未加载且 import 失败 → 静默关闭（_DET_SOLVER=False），不再尝试
        rn._DET_SOLVER = None
        out = rn._det_solve(_state("计算 6×7"), Deps(client=MockClient([])))
        # 真实 utils.det_solver 若已由子代理生成则 import 成功，答案取决于管线
        # 命中——两种结果（None 或 有效 det 结果）都可接受，但绝不能抛异常。
        if out is not None:
            self.assertEqual(out["reasoning_result"]["answer_source"], "det_solver")


if __name__ == "__main__":
    unittest.main()
