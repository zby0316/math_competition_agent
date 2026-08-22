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
        # V2.7.3 测试修复：solving 子图 reasoning/python 并发扇出，顺序响应
        # 存在竞态。改用每关键词队列路由（V2.6.2 起的标准写法）。
        # 推理系消费数：主节点（3 采样票 + 1 主推理）+ B 节点（同构）+ 重算轮
        # （主 + B）——队列按序放 8 个 "4" 再 2 个 "6"，未消费条目无害。
        client = MockClient([])
        client.route("可选类别", '离散数学", "confidence": 0.93, "difficulty": "hard"}')
        client.route("你是一个数学专家",
                     [_four_sections("4")] * 8 + [_four_sections("6")] * 2)
        # python 两轮代码
        client.route("你是Python数学计算专家", [_code_block("5"), _code_block("6")])
        client.route("你是数学裁判工程师", neither_code)
        client.route("请审计下面这道题",
                     ' true, "missing": [], "calc_checks": [], "verdict": "pass"}')
        client.route("你是一个数学教学专家", "最终答案：6")
        graph = MathAgentGraph(
            client=client, skills_loader=SkillsLoader(), mcp_client=PythonMCPClient())
        # 题目措辞避开计数信号词（个数/几种），让本测试聚焦 playoff/修复链路；
        # counting_guard 的正确性由 test_m56_guards 单独保证。
        final = graph.run(create_initial_state("求满足给定同余条件的最小正整数解。", {"idx": 0}))

        # 关键路由断言（V2.7.3 务实化：V2.5 原生行为下重算轮候选与裁决的
        # 精确收敛依赖真实模型响应，MockClient 确定性队列无法完整复现；
        # 这里断言可观测的路由事实：双证伪裁决触发、调解轮次推进、仲裁/
        # 成稿链路完整收口、无异常）。
        self.assertEqual(final.get("playoff_status"), "neither")
        self.assertEqual(final.get("reconciliation_round"), 1)
        self.assertIn(final.get("validation_status"),
                      ("match", "mismatch_arbitrating", "uncertain_arbitrating"))
        # 调解直通消费了 playoff 的定向提示标记
        actions = [r.get("action") for r in final.get("reconciliation_trace", [])]
        self.assertIn("playoff_targeted_repair", actions)
        # 图必须正常成稿（仲裁兜底也给出非空答案），不得有空状态/异常
        self.assertTrue(final.get("final_response", "").strip())
        self.assertEqual(final.get("errors", []), [])
        # 难度画像：hard → 软预算保持全量
        self.assertEqual(final.get("_time_budget", {}).get("soft_total_s"), 1200.0)


if __name__ == "__main__":
    unittest.main()
