"""治本机制集成测试（ultra_112 idx=13/40 根因）：

实算填空题不再走纯快速路径——扇出 Python 验证双路；当推理单采样算错、
Python 算对时，交叉验证 mismatch → playoff 代回复算裁决出正确答案。
"""
import unittest

from state.math_agent_state import create_initial_state
from utils.deps import MockClient
from utils.skills_loader import SkillsLoader
from utils.python_mcp_client import PythonMCPClient


class TestFillComputationDoublePath(unittest.TestCase):
    def test_fill_gets_python_verify_and_playoff_rescue(self):
        from langgraph_math_agent import MathAgentGraph

        # 填空：定积分 ∫₀¹ 2x dx = __。推理短路径答错(-2)，Python 算对(1)。
        responses = [
            '数学分析", "confidence": 0.95, "difficulty": "easy"}',   # 分类
            "答案：-2\n依据：计算略。",                                 # reasoning 短路径（错）
            "```python\nimport sympy as sp\nx=sp.symbols('x')\n"
            "print('最终答案:', sp.integrate(2*x,(x,0,1)))\n```",      # python（对：1）
            "```python\nimport sympy as sp\nx=sp.symbols('x')\n"
            "truth=int(sp.integrate(2*x,(x,0,1)))\n"
            'print("候选A核验:", "PASS" if -2==truth else "FAIL")\n'
            'print("候选B核验:", "PASS" if 1==truth else "FAIL")\n'
            'print("PLAYOFF_RESULT:", "B")\n```',                     # playoff 代回复算
            ' true, "missing": [], "calc_checks": [], "verdict": "pass"}',  # critic
            "最终答案：1",                                              # coordinator
        ]
        client = MockClient(responses)
        graph = MathAgentGraph(client=client, skills_loader=SkillsLoader(),
                               mcp_client=PythonMCPClient())
        final = graph.run(create_initial_state(
            "计算定积分 $\\int_0^1 2x\\,dx$ 的值，将结果填入空白。", {"idx": 13}))

        # 核心断言：实算填空题扇出了 Python（没跳过）
        po = final.get("python_output") or {}
        self.assertFalse(po.get("skipped", False), "实算填空题不应跳过 Python 验证")
        self.assertTrue(po.get("success"), "Python 验证应执行成功")
        # 冲突被 playoff 代回复算裁决，最终输出正确的 1 而非错的 -2
        self.assertEqual(final.get("playoff_status"), "decisive")
        self.assertIn("1", final.get("final_response", ""))
        self.assertNotIn("-2", final.get("final_response", ""))

    def test_concept_fill_still_fast_path(self):
        """纯概念填空（术语/定义）仍走快速路径，不扇出 Python。"""
        from graph.solving_subgraph import build_solving_subgraph  # noqa: F401
        from utils.verify_router import needs_python_verify
        self.assertFalse(needs_python_verify(
            "___ 是指样本方差分母的自由度概念。", "fill"))
        self.assertTrue(needs_python_verify(
            "计算该定积分的值，填入空白。", "fill"))
        self.assertFalse(needs_python_verify("求导数", "calc"))  # 非 fill 不介入


if __name__ == "__main__":
    unittest.main()
