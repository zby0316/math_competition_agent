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
        # V2.5 修复（det_solver 移植后）：solving 子图 reasoning/python 并发扇出，
        # MockClient 顺序响应存在线程竞态（det_solver 本地计算改变了相对时序）。
        # 改用关键词路由（backup-v255 同款写法）：每条路由按 prompt 特征词命中，
        # 与线程调度顺序无关。
        client = MockClient([])
        client.route("可选类别", '离散数学", "confidence": 0.93, "difficulty": "hard"}')
        # 推理系：hard 触发小 N 采样（3 票）+ 主推理 + B 异构路径（首轮，无 hint），
        # 重算轮（带 playoff 定向 hint）。MockClient 按插入顺序匹配关键词，
        # 最特定的路由先注册：
        # 1) Python 分支（prompt 同时含"你是Python数学计算专家"与 hint 文本）
        #    → PY 路由最优先，首轮 5、重算轮 6；
        # 2) 推理重算轮（含 hint）→ HINT 路由 → "6"；
        # 3) 推理首轮（无 hint）→ 通用 MATH 路由 → "4"。
        client.route("你是Python数学计算专家", [_code_block("5"), _code_block("6")])
        client.route("彻底更换解题方法重新独立求解",
                     [_four_sections("6")] * 2)
        client.route("你是一个数学专家", [_four_sections("4")] * 6)
        # Playoff 双证伪
        client.route("你是数学裁判工程师", neither_code)
        # Critic 放行
        client.route("你是数学解答审计员",
                     ' true, "missing": [], "calc_checks": [], "verdict": "pass"}')
        # Coordinator 成稿
        client.route("你是一个数学教学专家", "最终答案：6")
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
