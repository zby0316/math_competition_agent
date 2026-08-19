"""VeritasMath 全图集成测试：MockClient 驱动，真实 LangGraph 路由 + 真实代码执行。

两条主链路：
A. happy path——分类→双路求解一致→Critic 审计→coordinator 成稿；
B. 冲突裁决——推理与程序不一致→Playoff 代回复算裁决胜方→Critic→成稿。
无需 API key；Python 验证代码由真实执行器在子进程运行。
"""
import unittest

from state.math_agent_state import create_initial_state
from utils.deps import MockClient
from utils.skills_loader import SkillsLoader
from utils.python_mcp_client import PythonMCPClient


def _four_sections(answer: str) -> str:
    return (
        "## 问题分析\n这是一道基础计算题。\n\n"
        "## 详细解题步骤\n步骤1：直接计算。\n使用公式/定理：加法。\n计算过程：2+3。\n\n"
        f"## 最终答案\n{answer}\n\n"
        "## 关键验证点\n- 重新做一次加法\n"
    )


def _code_block(value_expr: str) -> str:
    return (
        "分析略。\n```python\n"
        "import sympy as sp\n"
        f"answer = {value_expr}\n"
        'print("验证状态:", "PASS")\n'
        'print("验证证据:", "直接计算")\n'
        'print("最终答案:", answer)\n```'
    )


def _run_graph(problem: str, responses: list = None, routes: list = None):
    """跑完整图。

    - ``responses``：顺序响应列表（无并发场景，如客观题 fast path）；
    - ``routes``：[(prompt 特征词, 响应)] 关键词路由——solving 子图
      reasoning/python 节点**并发**执行，共享顺序响应存在竞态（V2.5 增加
      sympy 前置计算后 python 线程可能抢先消费响应），路由按 prompt 特征
      命中即正确分发，消除竞态。
    """
    from langgraph_math_agent import MathAgentGraph
    from utils.time_budget import TimeBudget
    from utils.token_budget import TokenBudget
    client = MockClient(responses)
    for keyword, response in (routes or []):
        client.route(keyword, response)
    graph = MathAgentGraph(
        client=client, skills_loader=SkillsLoader(), mcp_client=PythonMCPClient())
    # 集成测试验证完整链路（match→critic→coordinator），预算必须给足：
    # 默认 TimeBudget 的 reserve=300s 会把 easy 题 soft_total(480s) 压到
    # remaining 180s < fast_path_threshold(300s)，触发 coordinator 跳过，
    # 破坏"match 直达成稿"的断言（历史遗留问题，非 V2.5 回归）。
    clock = TimeBudget(total_seconds=1200, reserve_seconds=0)
    return graph.run(create_initial_state(problem, {"idx": 0}),
                     time_budget=clock,
                     token_budget=TokenBudget()), client


_CLS = '数学分析", "confidence": 0.97, "difficulty": "easy"}'
_CRITIC_PASS = (' true, "missing": [], "calc_checks": [{"check": "2+3=5", "ok": true}],'
                ' "verdict": "pass"}')


class TestHappyPathWithCritic(unittest.TestCase):
    def test_match_goes_through_critic_to_coordinator(self):
        routes = [
            ("可选类别", _CLS),
            ("你是一个数学专家，使用", _four_sections("5")),
            ("你是Python数学计算专家", _code_block("2+3")),
            ("请审计下面这道题", _CRITIC_PASS),
            ("你是一个数学教学专家", "最终答案：5"),
        ]
        final, client = _run_graph("计算 2+3 的值。", routes=routes)
        self.assertEqual(final.get("validation_status"), "match")
        # Critic 必须真的运行过且放行
        self.assertEqual(final.get("critic_status"), "pass")
        self.assertTrue(final.get("final_response", "").strip())
        self.assertIn("5", final.get("final_response", ""))
        # 难度画像已应用（easy → 软预算 480）
        self.assertEqual(final.get("difficulty"), "easy")
        self.assertEqual(final.get("_time_budget", {}).get("soft_total_s"), 480.0)
        # 数学分析不在高风险领域：不应扇出第二推理路径
        self.assertIsNone(final.get("reasoning_result_alt"))


class TestPlayoffDecisive(unittest.TestCase):
    def test_mismatch_routes_to_playoff_and_locks_winner(self):
        playoff_code = (
            "```python\n"
            "# 代回核验：候选A=5 候选B=6，真值由独立计算给出\n"
            "truth = 2 + 3\n"
            'print("候选A核验:", "PASS" if 5 == truth else "FAIL 5 != %s" % truth)\n'
            'print("候选B核验:", "PASS" if 6 == truth else "FAIL 6 != %s" % truth)\n'
            'print("PLAYOFF_RESULT:", "A")\n```'
        )
        routes = [
            ("可选类别", _CLS),
            ("你是一个数学专家，使用", _four_sections("5")),
            ("你是Python数学计算专家", _code_block("3+3")),
            ("你是数学裁判工程师", playoff_code),
            ("请审计下面这道题", _CRITIC_PASS),
            ("你是一个数学教学专家", "最终答案：5"),
        ]
        final, client = _run_graph("计算 2+3 的值。", routes=routes)
        self.assertEqual(final.get("validation_status"), "mismatch_playoff")
        self.assertEqual(final.get("playoff_status"), "decisive")
        self.assertEqual(final.get("playoff_answer"), "5")
        self.assertEqual(final.get("validated_answer"), "5")
        self.assertEqual(final.get("critic_status"), "pass")
        self.assertIn("5", final.get("final_response", ""))


class TestObjectiveFastPath(unittest.TestCase):
    def test_objective_skips_python_and_critic_llm(self):
        responses = [
            '统计推断", "confidence": 0.95, "difficulty": "easy"}',
            "答案：B\n依据：按定义逐项核对，仅 B 满足题面条件。",  # 客观题短契约
            # 无 Python、无 Critic LLM（客观题直通）、coordinator 不耗 LLM（短路径）
        ]
        final, client = _run_graph(
            "样本方差的分母是（ ）。A. n B. n-1 C. n+1 D. 1", responses)
        self.assertEqual(final.get("validation_status"), "match")
        self.assertTrue(final.get("python_output", {}).get("skipped", False)
                        or not final.get("python_output", {}).get("success"))
        # Critic 对客观题只走确定性直通
        self.assertEqual(final.get("critic_status"), "pass")
        self.assertIn("B", final.get("final_response", ""))
        # 只应消耗 2 次 LLM 调用（分类 + 客观短答）
        self.assertEqual(len(client.calls), 2)


if __name__ == "__main__":
    unittest.main()
