"""VeritasMath 升级机制的离线回归测试（无需 API key，不访问在线模型）。

覆盖：Critic 审计解析、模结构守护、Playoff 裁决解析、难度软预算、
matcher 加固（符号对齐/限时）、分类难度解析、异构路径调度、执行器安全拦截。
"""
import time
import unittest

from config import CONFIG


class TestCriticAudit(unittest.TestCase):
    def test_parse_prefilled_fragment(self):
        from utils.critic_audit import parse_critic_verdict
        # chat_prefilled 已把种子 '{"complete":' 与续写拼接成完整 JSON
        raw = '{"complete": false, "missing": ["第二问概率"], "calc_checks": [], "verdict": "incomplete"}'
        verdict = parse_critic_verdict(raw)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["verdict"], "incomplete")
        self.assertEqual(verdict["missing"], ["第二问概率"])

    def test_parse_wrapped_and_garbage(self):
        from utils.critic_audit import parse_critic_verdict
        wrapped = '说明文字...\n{"complete": true, "verdict": "pass", "missing": []}\n尾部'
        self.assertEqual(parse_critic_verdict(wrapped)["verdict"], "pass")
        self.assertIsNone(parse_critic_verdict("完全不是 JSON"))
        self.assertIsNone(parse_critic_verdict(""))

    def test_verdict_inference_and_merge_direction(self):
        from utils.critic_audit import merge_with_deterministic, parse_critic_verdict
        # LLM 漏写 verdict 时按 complete/missing 推断
        inferred = parse_critic_verdict('{"complete": false, "missing": ["区间上限"]}')
        self.assertEqual(inferred["verdict"], "incomplete")
        # LLM 说 pass 但确定性契约报缺 → 以契约为准（保守方向）
        merged = merge_with_deterministic(
            parse_critic_verdict('{"complete": true, "verdict": "pass"}'), ["检验结论"])
        self.assertEqual(merged["verdict"], "incomplete")
        self.assertIn("检验结论", merged["missing"])
        # LLM 报 calc_error 即使契约无意见也成立
        merged2 = merge_with_deterministic(
            parse_critic_verdict('{"complete": true, "verdict": "calc_error"}'), [])
        self.assertEqual(merged2["verdict"], "calc_error")
        # 双方都无意见 → pass；LLM 不可用且契约无缺失 → pass（不制造阻塞）
        self.assertEqual(merge_with_deterministic(None, [])["verdict"], "pass")

    def test_repair_hint_contains_missing(self):
        from utils.critic_audit import build_repair_hint
        hint = build_repair_hint({"missing": ["最小面积"], "calc_checks": []}, "y=x/2+1/2")
        self.assertIn("最小面积", hint)
        self.assertIn("y=x/2+1/2", hint)


class TestModularGuard(unittest.TestCase):
    def test_detect_f2_aggregation(self):
        from utils.modular_guard import detect_modular_context
        hit = detect_modular_context("设 f: Q → \\mathbb{F}_2，求六个函数值之和。")
        self.assertTrue(hit["hit"])

    def test_no_aggregation_no_hit(self):
        from utils.modular_guard import detect_modular_context
        # 只提 F_2 而无聚合动作：不触发
        self.assertFalse(detect_modular_context("证明该映射是 F_2 线性的。")["hit"])
        # 普通实数求和：不触发
        self.assertFalse(detect_modular_context("求函数在区间上的最大值与总和。")["hit"])

    def test_prompt_clause_and_code_compliance(self):
        from utils.modular_guard import code_complies, prompt_clause
        problem = "函数取值于 F_2，求所有函数值的总和。"
        clause = prompt_clause(problem)
        self.assertIn("模结构强制条款", clause)
        self.assertTrue(code_complies("total = sum(vals) % 2\nprint(total)"))
        self.assertTrue(code_complies("from galois import GF\nGF2 = GF(2)"))
        self.assertFalse(code_complies("total = sum(vals)\nprint(total)"))
        self.assertEqual(prompt_clause("1+1 等于几？"), "")


class TestPlayoff(unittest.TestCase):
    def test_parse_result_marker(self):
        from nodes.playoff_node import parse_playoff_result
        self.assertEqual(parse_playoff_result("...\nPLAYOFF_RESULT: B\n"), "B")
        self.assertEqual(parse_playoff_result("PLAYOFF_RESULT: A\nPLAYOFF_RESULT: BOTH"), "BOTH")
        self.assertEqual(parse_playoff_result("PLAYOFF_RESULT: NEITHER"), "NEITHER")

    def test_parse_result_inferred_from_checks(self):
        from nodes.playoff_node import parse_playoff_result
        self.assertEqual(parse_playoff_result("候选A核验: PASS\n候选B核验: FAIL x"), "A")
        self.assertEqual(parse_playoff_result("候选A核验: PASS\n候选B核验: PASS"), "BOTH")
        self.assertEqual(parse_playoff_result("没有任何标记"), "INCONCLUSIVE")

    def test_candidates_priority(self):
        from nodes.playoff_node import playoff_candidates
        state = {
            "reasoning_result": {"answer": "5"},
            "python_output": {"answer": "6"},
            "reasoning_result_alt": {"answer": "7"},
        }
        self.assertEqual(playoff_candidates(state), ("5", "6"))
        # Python 缺席 → 主推理 vs 第二推理
        self.assertEqual(
            playoff_candidates({"reasoning_result": {"answer": "5"},
                                "python_output": {"answer": ""},
                                "reasoning_result_alt": {"answer": "7"}}),
            ("5", "7"))
        # 单候选 → 无季后赛
        self.assertEqual(playoff_candidates({"reasoning_result": {"answer": "5"}}), ("", ""))


class TestDifficultyBudget(unittest.TestCase):
    def test_profile_only_tightens(self):
        from utils.time_budget import TimeBudget
        budget = TimeBudget(total_seconds=1200, reserve_seconds=300)
        budget.apply_difficulty_profile("easy")
        # V2.6.3 回滚：easy 600→480（官方慢 API 下 600 是纯增量墙钟）
        self.assertEqual(budget.soft_total, 480.0)
        # 后到的更难画像不会把已收紧的预算放回去
        budget.apply_difficulty_profile("hard")
        self.assertEqual(budget.soft_total, 480.0)
        self.assertEqual(budget.difficulty_profile, "easy")

    def test_remaining_uses_soft_total_but_hard_untouched(self):
        from utils.time_budget import TimeBudget
        budget = TimeBudget(total_seconds=1200, reserve_seconds=300)
        budget.apply_difficulty_profile("medium")
        self.assertLessEqual(budget.remaining(), 840 - 300 + 1)
        self.assertGreater(budget.remaining_hard(), 1100)  # 硬限不变
        snap = budget.snapshot()
        self.assertEqual(snap["soft_total_s"], 840.0)
        self.assertEqual(snap["total_s"], 1200.0)

    def test_unknown_difficulty_noop(self):
        from utils.time_budget import TimeBudget
        budget = TimeBudget(total_seconds=1200, reserve_seconds=300)
        budget.apply_difficulty_profile("impossible")
        self.assertEqual(budget.soft_total, 1200.0)


class TestMatcherHardening(unittest.TestCase):
    def test_run_limited_timeout(self):
        from utils.answer_matcher import _run_limited
        self.assertIsNone(_run_limited(lambda: time.sleep(3), 0.2))
        self.assertEqual(_run_limited(lambda: 42, 2), 42)

    def test_symbolic_equivalent_latex_pair(self):
        # 评委报告 Q1：等价式曾因 simplify 判不了而落相似度付仲裁费
        from utils.answer_matcher import AnswerMatcher
        verdict = AnswerMatcher._sympy_equivalent(
            "4*pi*(32-a**5)/5", r"$\frac{4\pi}{5}(32-a^5)$", 1e-6)
        self.assertTrue(verdict)

    def test_symbol_alignment_renamed_symbols(self):
        # 同构不同名：2x+1 与 2a+1 应在符号对齐采样下判等价
        from utils.answer_matcher import AnswerMatcher
        verdict = AnswerMatcher._numeric_sample_with_alignment(
            __import__("sympy").sympify("2*x+1"),
            __import__("sympy").sympify("2*a+1"), 1e-6)
        self.assertTrue(verdict)
        # 不同结构不得误判等价
        verdict2 = AnswerMatcher._numeric_sample_with_alignment(
            __import__("sympy").sympify("2*x+1"),
            __import__("sympy").sympify("3*a+1"), 1e-6)
        self.assertFalse(verdict2)

    def test_plain_inequivalent(self):
        from utils.answer_matcher import AnswerMatcher
        self.assertFalse(AnswerMatcher._sympy_equivalent("5", "6", 1e-6))

    def test_list_shaped_answers_never_crash(self):
        # V2.7 真机事故回归：hard12 idx=11/12 的 list 形态答案在
        # _numeric_sample_equivalent 里 AttributeError 崩溃，整题被
        # error_handler 替换为空状态、连出厂都失败。必须保守返回 None
        # （交字符串相似度通道），绝不抛异常。
        from utils.answer_matcher import AnswerMatcher
        import sympy as sp
        lst = sp.sympify("[1, 1, 5, 5]")  # sympify 可产出 list
        self.assertIsNone(AnswerMatcher._numeric_sample_equivalent(
            lst, sp.sympify("x"), 1e-6))
        self.assertIsNone(AnswerMatcher._numeric_sample_equivalent(
            sp.sympify("x"), lst, 1e-6))
        self.assertIsNone(AnswerMatcher._numeric_sample_with_alignment(
            lst, lst, 1e-6))
        d = AnswerMatcher._match_computation_detailed(
            "[1, 1, 5, 5, 5, 5, 9, 9, 10, 10, 16]", "1, 1, 5, 5, 5, 5, 9, 9, 10, 10, 16")
        self.assertIsInstance(d, dict)
        self.assertIn("verdict", d)

    def test_function_label_stripped(self):
        # 真机压测发现：`\text{tr}(A^3) = 48$` 与 `48` 曾因标签无法消费落仲裁
        from utils.answer_matcher import AnswerMatcher
        d = AnswerMatcher._match_computation_detailed(
            "$\\text{tr}(A^3) = 48$", "48")
        self.assertTrue(d["verdict"], d.get("reason"))
        d2 = AnswerMatcher._match_computation_detailed(
            "$\\mathrm{Var}(X) = 2$", "2")
        self.assertTrue(d2["verdict"], d2.get("reason"))

    def test_equation_shaped_answer_vs_bare_value(self):
        # ultra_112 真机压测实证的 10 组真实答案对（修复后全部正确判定）
        from utils.answer_matcher import AnswerMatcher
        pairs = [
            ("$\\det(A) = 0$", "det(A) = 0", True),
            ("$\\int_0^\\infty x e^{-x} dx = 1$", "1", True),
            ("$\\int_1^e \\ln x \\, dx = 1$", "1", True),
            ("$y(x) = (1+x)e^{-x}$", "y(x) = (x + 1)*exp(-x)", True),
            ("$\\sum_{n=1}^{\\infty} \\frac{1}{n(n+1)} = 1$", "1", True),
            ("P(Z>1) = 1/2", "0.5", True),
            ("$\\binom{5}{2} = 10$", "10", True),
            ("5", "6", False),
        ]
        for a, b, expect in pairs:
            with self.subTest(a=a[:30], b=b[:20]):
                d = AnswerMatcher._match_computation_detailed(a, b)
                self.assertEqual(d["verdict"], expect, d.get("reason"))
        # 方程求解类不得被误剥（x^2+1=2 的两处等号都在顶层，形态 3 不触发）
        d_eq = AnswerMatcher._match_computation_detailed("x^2+1=2 的解为 x=1", "1")
        self.assertNotEqual(d_eq["method"], "exact")
        # 和式上下限内的等号不得算顶层等号
        self.assertEqual(AnswerMatcher._match_computation_detailed(
            "$\\sum_{n=1}^{10} n = 55$", "55")["verdict"], True)


class TestClassifierDifficulty(unittest.TestCase):
    def test_extract_difficulty(self):
        from nodes.classifier_node import _extract_decision, _extract_difficulty
        raw = '数学分析", "confidence": 0.95, "difficulty": "hard"}'
        self.assertEqual(_extract_difficulty(raw), "hard")
        decision = _extract_decision(raw, ["数学分析", "高等代数"])
        self.assertEqual(decision[0], "数学分析")
        self.assertEqual(decision[2], "hard")
        self.assertIsNone(_extract_difficulty('数学分析", "confidence": 0.9}'))


class TestDiverseReasoningDispatch(unittest.TestCase):
    def test_variant_b_maps_to_alt_keys(self):
        import importlib
        mod = importlib.import_module("nodes.reasoning_agent_node")
        captured = {}

        def fake_impl(state, config):
            captured["hint"] = state.get("branch_hint", "")
            return {"reasoning_result": {"answer": "42", "steps": []},
                    "reasoning_trace": [{"attempt": 1}],
                    "reasoning_attempts": 1,
                    "reasoning_raw_response": "raw"}

        original = mod._reasoning_agent_impl
        mod._reasoning_agent_impl = fake_impl
        try:
            out = mod.reasoning_agent_node(
                {"method_variant": "B", "question_mode": "computation",
                 "branch_hint": "上次漏了第二问"}, {})
        finally:
            mod._reasoning_agent_impl = original
        self.assertIn("第二独立解法", captured["hint"])
        self.assertIn("上次漏了第二问", captured["hint"])
        self.assertEqual(out["reasoning_result_alt"]["answer"], "42")
        self.assertNotIn("reasoning_result", out)

    def test_variant_b_skips_objective(self):
        import importlib
        mod = importlib.import_module("nodes.reasoning_agent_node")
        out = mod.reasoning_agent_node(
            {"method_variant": "B", "question_mode": "choice"}, {})
        self.assertIsNone(out["reasoning_result_alt"])
        self.assertEqual(out["reasoning_trace_alt"][0]["status"], "skipped")

    def test_primary_path_passthrough(self):
        import importlib
        mod = importlib.import_module("nodes.reasoning_agent_node")
        original = mod._reasoning_agent_impl
        mod._reasoning_agent_impl = lambda state, config: {"reasoning_result": {"answer": "1"}}
        try:
            out = mod.reasoning_agent_node({"method_variant": ""}, {})
        finally:
            mod._reasoning_agent_impl = original
        self.assertEqual(out["reasoning_result"]["answer"], "1")


class TestExecutorHardening(unittest.TestCase):
    def test_blocked_imports(self):
        from mcp_servers.python_executor.server import _static_safety_scan
        self.assertIn("subprocess", _static_safety_scan("import subprocess\nsubprocess.run([])"))
        self.assertIn("socket", _static_safety_scan("from socket import socket"))
        self.assertTrue(_static_safety_scan("os.system('dir')"))
        self.assertTrue(_static_safety_scan("open('x.txt','w')"))
        self.assertTrue(_static_safety_scan("eval('1+1')"))

    def test_allowed_math_code(self):
        from mcp_servers.python_executor.server import _static_safety_scan
        self.assertEqual(_static_safety_scan(
            "import sys\nsys.setrecursionlimit(10000)\nimport sympy as sp\n"
            "print('最终答案:', sp.Integer(2)+3)"), "")

    def test_execute_blocked_returns_failure(self):
        from mcp_servers.python_executor.server import execute_python
        out = execute_python("import os\nos.system('echo hi')\nprint('最终答案: 1')", timeout=5)
        self.assertFalse(out["success"])
        self.assertIn("静态安全检查", out["stderr"])

    def test_execute_real_code(self):
        from mcp_servers.python_executor.server import execute_python
        out = execute_python("print('最终答案:', 2+3)", timeout=15)
        self.assertTrue(out["success"])
        self.assertEqual(out["answer"], "5")


class TestArbiterThreeCandidates(unittest.TestCase):
    def test_pool_includes_alt(self):
        from nodes.semantic_arbiter_node import _candidate_pool
        state = {
            "problem": "p",
            "reasoning_result": {"answer": "5", "analysis": "a", "steps": []},
            "python_output": {"answer": "6", "success": True, "stdout": "最终答案: 6"},
            "reasoning_result_alt": {"answer": "7", "analysis": "b", "steps": []},
        }
        pool = _candidate_pool(state)
        sources = {c.source for c in pool}
        self.assertEqual(sources, {"reasoning", "python", "reasoning_alt"})

    def test_pool_dedupes_identical_answers(self):
        from nodes.semantic_arbiter_node import _candidate_pool
        state = {
            "problem": "p",
            "reasoning_result": {"answer": "5"},
            "python_output": {"answer": "5", "success": True, "stdout": "最终答案: 5"},
            "reasoning_result_alt": {"answer": "5"},
        }
        self.assertEqual(len(_candidate_pool(state)), 1)


class TestReconciliationPassthrough(unittest.TestCase):
    def test_critic_hint_passthrough(self):
        from nodes.reconciliation_node import reconciliation_node
        state = {
            "reconciliation_round": 0,
            "repair_hint_source": "critic",
            "reasoning_retry_hint": "定向修复提示R",
            "python_retry_hint": "定向修复提示P",
            "python_output": {"success": True, "answer": "1"},
        }
        out = reconciliation_node(state, {"configurable": {"deps": None}})
        self.assertEqual(out["next_node"], "solving")
        self.assertEqual(out["reasoning_retry_hint"], "定向修复提示R")
        self.assertIsNone(out["repair_hint_source"])
        self.assertEqual(out["reconciliation_round"], 1)


if __name__ == "__main__":
    unittest.main()
