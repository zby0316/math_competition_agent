# -*- coding: utf-8 -*-
"""V2.6 深度反思环：正确性批判性复核（Critic 前）。

验证：触发条件（hard/低置信/客观题跳过/已反思跳过）+ verdict 解析 +
路由（correct→critic，wrong→reconciliation，无答案→critic）+ 修复提示。
"""
import unittest
from unittest import mock

from config import CONFIG


class TestShouldReflect(unittest.TestCase):
    """触发条件：所有非客观题触发一次（不依赖分类器 difficulty/confidence）。"""

    def _should(self, state):
        from nodes.reflection_node import should_reflect
        return should_reflect(state)

    def test_computation_triggers(self):
        state = {"question_mode": "computation", "reflection_round": 0,
                 "difficulty": "easy", "category_confidence": 0.98}
        self.assertTrue(self._should(state))

    def test_proof_triggers(self):
        state = {"question_mode": "proof", "reflection_round": 0,
                 "difficulty": "hard", "category_confidence": 0.5}
        self.assertTrue(self._should(state))

    def test_high_confidence_triggers(self):
        # 高置信也反思：实测分类器把科研级题判 0.98 高置信却照样答错
        state = {"question_mode": "computation", "reflection_round": 0,
                 "difficulty": "easy", "category_confidence": 0.99}
        self.assertTrue(self._should(state))

    def test_objective_skips(self):
        state = {"question_mode": "choice", "reflection_round": 0,
                 "difficulty": "hard", "category_confidence": 0.5}
        self.assertFalse(self._should(state))

    def test_fill_skips(self):
        state = {"question_mode": "fill", "reflection_round": 0,
                 "difficulty": "hard", "category_confidence": 0.5}
        self.assertFalse(self._should(state))

    def test_already_reflected_skips(self):
        state = {"question_mode": "computation", "reflection_round": 1,
                 "difficulty": "hard", "category_confidence": 0.5}
        self.assertFalse(self._should(state))


class TestParseReflectionVerdict(unittest.TestCase):
    """verdict 解析：correct/wrong/容错/失败。"""

    def _parse(self, raw):
        from nodes.reflection_node import parse_reflection_verdict
        return parse_reflection_verdict(raw)

    def test_correct(self):
        v = self._parse('{"verdict": "correct", "error_step": "", '
                        '"fix_hint": "", "reason": "代回成立"}')
        self.assertEqual(v["verdict"], "correct")

    def test_wrong_with_hints(self):
        v = self._parse('{"verdict": "wrong", "error_step": "差一", '
                        '"fix_hint": "序号从1起数", "reason": "第k大差一"}')
        self.assertEqual(v["verdict"], "wrong")
        self.assertEqual(v["fix_hint"], "序号从1起数")

    def test_prefill_fragment_tolerated(self):
        # 模型可能在 prefill 后插入说明文字
        v = self._parse('{"verdict":"correct" 说明文字 "error_step":"", "fix_hint":"", "reason":"x"}')
        self.assertIsNone(v)  # 无法解析成完整 JSON → None（放行，不误判）

    def test_invalid_verdict_returns_none(self):
        v = self._parse('{"verdict": "maybe", "error_step": "", '
                        '"fix_hint": "", "reason": ""}')
        self.assertIsNone(v)


class TestDeAnchor(unittest.TestCase):
    """fix_hint 去锚定：剥离"应为 X"类数值锚定，保留错误性质。"""

    def _de(self, text):
        from nodes.reflection_node import _de_anchor
        return _de_anchor(text)

    def test_anchor_stripped(self):
        self.assertNotIn("198", self._de("正确结果应为 198 而非 748"))
        self.assertNotIn("8", self._de("应为 8 而非 12"))
        self.assertNotIn("-1/2", self._de("符号反了，应为 -1/2"))

    def test_error_nature_preserved(self):
        self.assertIn("符号反了", self._de("符号反了，应为 -1/2"))
        self.assertIn("差一", self._de("差一，序号从1起数"))
        self.assertIn("未检验端点", self._de("未检验端点"))


class TestReflectionNodeRouting(unittest.TestCase):
    """路由：correct→critic；wrong 且可重算→reconciliation；无答案→critic。"""

    def _run(self, state, mock_verdict, sympy_verify=None):
        from nodes import reflection_node
        # 构造 config 以注入 deps（TimeBudget 可负担、非 fast_path）
        deps = mock.MagicMock()
        deps.time_budget = mock.MagicMock()
        deps.time_budget.remaining_hard.return_value = 600.0
        deps.time_budget.fast_path.return_value = False
        deps.time_budget.can_afford.return_value = True
        deps.token_budget = None
        deps.logger = mock.MagicMock()
        deps.client = mock.MagicMock()
        config = {"configurable": {"deps": deps}}
        with mock.patch("nodes.reflection_node._llm_reflect",
                        return_value=mock_verdict), \
             mock.patch("nodes.reflection_node._sympy_verify",
                        return_value=sympy_verify):
            return reflection_node(state, config)

    def _state(self):
        return {
            "question_mode": "computation", "difficulty": "hard",
            "category_confidence": 0.5, "reflection_round": 0,
            "problem": "求 x^2=4 的解",
            "validated_answer": "x=2",
            "reasoning_result": {"answer": "x=2"},
            "python_output": {},
        }

    def test_correct_routes_to_coordinator(self):
        out = self._run(self._state(),
                        {"verdict": "correct", "error_step": "",
                         "fix_hint": "", "reason": "ok"})
        self.assertEqual(out["next_node"], "coordinator")
        self.assertEqual(out["reflection_status"], "correct")

    def test_sympy_verify_fail_triggers_retry(self):
        """V2.7：LLM 反思判 correct，但 SymPy 代回 FAIL → 触发重算（抓漏判）。"""
        out = self._run(self._state(),
                        {"verdict": "correct", "error_step": "",
                         "fix_hint": "", "reason": "ok"},
                        sympy_verify="FAIL")
        self.assertEqual(out["next_node"], "reconciliation")
        self.assertEqual(out["reflection_status"], "wrong")

    def test_sympy_verify_pass_still_coordinator(self):
        """SymPy 代回 PASS → 正常放行。"""
        out = self._run(self._state(),
                        {"verdict": "correct", "error_step": "",
                         "fix_hint": "", "reason": "ok"},
                        sympy_verify="PASS")
        self.assertEqual(out["next_node"], "coordinator")

    def test_wrong_routes_to_reconciliation(self):
        state = self._state()
        out = self._run(state,
                        {"verdict": "wrong", "error_step": "漏了负根",
                         "fix_hint": "补 x=-2", "reason": "平方方程双根"})
        self.assertEqual(out["next_node"], "reconciliation")
        self.assertEqual(out["repair_hint_source"], "reflection")
        self.assertIn("x=-2", out["reasoning_retry_hint"])

    def test_no_answer_routes_to_coordinator(self):
        state = self._state()
        state["validated_answer"] = ""
        state["reasoning_result"] = {"answer": ""}
        out = self._run(state, None)
        self.assertEqual(out["next_node"], "coordinator")
        self.assertEqual(out["reflection_status"], "skipped")

    def test_reflect_unavailable_routes_to_coordinator(self):
        out = self._run(self._state(), None)  # _llm_reflect 返回 None
        self.assertEqual(out["next_node"], "coordinator")

    def test_repair_hint_source_consumed_by_reconciliation(self):
        """reconciliation 认可 reflection 作为定向修复来源。"""
        import inspect
        import nodes.reconciliation_node as mod
        src = inspect.getsource(mod)
        self.assertIn('"reflection"', src)


if __name__ == "__main__":
    unittest.main()
