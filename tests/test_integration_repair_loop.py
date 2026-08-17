"""修复回路集成测试（VeritasMath 核心增值链路）：

高风险领域（离散数学）首轮扇出主推理+异构路径+Python 三路；
两路冲突 → Playoff 复算双证伪 → 调解直通定向提示 → 换方法重算翻盘
→ 三路一致 → Critic 放行 → 成稿。全程 MockClient + 真实执行器。
"""
import unittest

from state.math_agent_state import create_initial_state
from utils.deps import MockClient
from utils.skills_loader import SkillsLoader
from utils.python_mcp_client import PythonMCPClient
from tests.test_integration_graph import _code_block, _four_sections


class TestNeitherThenDiverseRescue(unittest.TestCase):
    def test_full_repair_loop(self):
        from langgraph_math_agent import MathAgentGraph

        neither_code = (
            "```python\ntruth = 6\n"
            'print("候选A核验:", "PASS" if 4 == truth else "FAIL")\n'
            'print("候选B核验:", "PASS" if 5 == truth else "FAIL")\n'
            'print("PLAYOFF_RESULT:", "NEITHER")\n```'
        )
        responses = [
            # 分类：高风险领域 + hard（首轮即扇出三路）
            '离散数学", "confidence": 0.93, "difficulty": "hard"}',
            # V2.9 多温度采样：hard 题 A 路径先做 6 次分层温度采样（都答 4，
            # 投票一致 consensus=1.0，胜出 4）
            _four_sections("4"),
            _four_sections("4"),
            _four_sections("4"),
            _four_sections("4"),
            _four_sections("4"),
            _four_sections("4"),
            # 首轮异构路径 B：4、Python：5 —— 三方全不一致（4/4/5）
            _four_sections("4"),
            _code_block("5"),
            # Playoff 裁决代码：双证伪
            neither_code,
            # 重算轮（换方法提示 + 异构路径）：三路收敛到 6（重算轮 reconciliation>=1
            # 不再多温度采样，A 单次直答）
            _four_sections("6"),
            _four_sections("6"),
            _code_block("6"),
            # Critic 放行 + coordinator 成稿
            ' true, "missing": [], "calc_checks": [], "verdict": "pass"}',
            "最终答案：6",
        ]
        client = MockClient(responses)
        graph = MathAgentGraph(
            client=client, skills_loader=SkillsLoader(), mcp_client=PythonMCPClient())
        # 题目措辞避开计数信号词（个数/几种），让本测试聚焦 playoff/修复链路；
        # counting_guard 的正确性由 test_m56_guards 单独保证。
        final = graph.run(create_initial_state("求满足给定同余条件的最小正整数解。", {"idx": 0}))

        # 关键路由断言
        self.assertEqual(final.get("playoff_status"), "neither")
        self.assertEqual(final.get("reconciliation_round"), 1)
        self.assertEqual(final.get("validation_status"), "match")
        self.assertEqual(final.get("critic_status"), "pass")
        self.assertIn("6", final.get("final_response", ""))
        # 异构第二路径确实被扇出并产出（首轮与重算轮各一次）
        self.assertIsNotNone(final.get("reasoning_result_alt"))
        self.assertEqual(final["reasoning_result_alt"].get("answer"), "6")
        # 调解直通消费了 playoff 的定向提示标记
        actions = [r.get("action") for r in final.get("reconciliation_trace", [])]
        self.assertIn("playoff_targeted_repair", actions)
        # 难度画像：hard → 软预算保持全量
        self.assertEqual(final.get("_time_budget", {}).get("soft_total_s"), 1200.0)


if __name__ == "__main__":
    unittest.main()
