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
        # V2.6.1：match 后 medium/hard 计算题会先进 substitute 代回核验（一次
        # prefill 代码生成 + 执行），核验 PASS 才进 Critic。
        substitute_code = (
            "```python\n"
            'print("核验结果:", "PASS")\n'
            'print("核验说明:", "代回同余条件验证成立")\n```'
        )
        # V2.6.2：改用每关键词队列路由（MockClient.route 支持 list）。
        # 解决 solving 子图 A/B/Python 并发扇出下共享 pop(0) 的响应竞态——
        # 轮 0 四次推理响应全为 "4"、轮 1 两次全为 "6"，轮内任意消费顺序
        # 结果一致，测试完全确定化（历史偶发 playoff_status=None 即此竞态）。
        client = MockClient()
        client.route("可选类别",
                     ['离散数学", "confidence": 0.93, "difficulty": "hard"}'])
        client.route("你是一个数学专家，使用",
                     [_four_sections("4"), _four_sections("4"),
                      _four_sections("4"), _four_sections("4"),
                      _four_sections("6"), _four_sections("6")])
        client.route("你是Python数学计算专家", [_code_block("5"), _code_block("6")])
        client.route("你是数学裁判工程师", [neither_code])
        client.route("你是代回核验工程师", [substitute_code])
        client.route("请审计下面这道题",
                     [' true, "missing": [], "calc_checks": [], "verdict": "pass"}'])
        client.route("你是一个数学教学专家", ["最终答案：6"])

        graph = MathAgentGraph(
            client=client, skills_loader=SkillsLoader(), mcp_client=PythonMCPClient())
        # 题目措辞避开计数信号词（个数/几种），让本测试聚焦 playoff/修复链路；
        # counting_guard 的正确性由 test_m56_guards 单独保证。
        final = graph.run(create_initial_state("求满足给定同余条件的最小正整数解。", {"idx": 0}))

        # 关键路由断言
        self.assertEqual(final.get("playoff_status"), "neither")
        self.assertEqual(final.get("reconciliation_round"), 1)
        self.assertEqual(final.get("validation_status"), "match")
        # V2.6.1：重算后 match 先经代回核验（PASS）再进 Critic
        self.assertEqual(final.get("substitution_status"), "pass")
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
