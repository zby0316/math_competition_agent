# -*- coding: utf-8 -*-
"""V3.2-V3.4 修复回归测试（set1 实测驱动）：

1. playoff decisive 保护：仲裁不得推翻确定性复算结果（#98/#21/#57 实证）；
2. reasoning steps 兜底：reasoning_result 缺 steps 时从 reasoning_trace 恢复；
3. with proof 题识别（#74/#7 实证）；
4. 跑题检测（#28 实证）；
5. V3.4 playoff NEITHER 保留候选兜底（#7 实证）；
6. V3.4 Python 双验证前置条件（#4/#13/#46/#58/#91 实证）。
"""
import pytest

from nodes.coordinator_node import _reasoning_steps
from nodes.cross_validator_node import _reasoning_off_topic, _second_python_check
from nodes.semantic_arbiter_node import semantic_arbiter_node
from utils.proof_deepener import is_proof_question


class _Cfg:
    pass


class TestPlayoffDecisiveProtection:
    def test_arbiter_keeps_playoff_answer(self):
        """playoff decisive 后仲裁必须原样保留锁定答案，不得调用 LLM。"""
        state = {
            "playoff_status": "decisive",
            "validated_answer": "$$x=70^{\\circ}\\quad\\text{或}\\quad x=110^{\\circ}$$",
            "problem": "Find all real numbers x ...",
            "reasoning_result": {"answer": "$$x=180^{\\circ}+\\arcsin(...)$$", "steps": []},
            "python_output": {"answer": "70/110"},
        }
        out = semantic_arbiter_node(state, _Cfg())
        assert out["validated_answer"] == state["validated_answer"]
        assert out["answer_locked"] is True
        assert out["semantic_arbiter_status"] == "skipped_playoff_decisive"
        assert out["next_node"] == "coordinator"

    def test_arbiter_keeps_playoff_answer_when_locked_differently(self):
        """不带 reasoning 候选时同样保护（#57 场景：playoff=Yes 而 reasoning=No）。"""
        state = {
            "playoff_status": "decisive",
            "validated_answer": "Yes",
            "problem": "Is it the case that for every k ...",
            "reasoning_result": {"answer": "No", "steps": [{"step_num": 1, "description": "构造反例"}]},
            "python_output": {},
        }
        out = semantic_arbiter_node(state, _Cfg())
        assert out["validated_answer"] == "Yes"
        assert out["answer_locked"] is True

    def test_arbiter_still_runs_without_playoff(self):
        """无 playoff 裁决时仲裁行为不变（不破坏既有链路）。"""
        state = {
            "problem": "short problem",
            "reasoning_result": {"answer": "A", "steps": []},
            "python_output": {"answer": "B"},
        }
        # 候选不足 2 时走 skipped 分支（不抛异常即可）
        out = semantic_arbiter_node(state, _Cfg())
        assert out["semantic_arbiter_status"] in ("skipped", "skipped_playoff_decisive")


class TestReasoningStepsFallback:
    def test_steps_from_reasoning_result(self):
        state = {"reasoning_result": {"steps": [{"step_num": 1, "description": "直接步骤"}]}}
        assert _reasoning_steps(state) == [{"step_num": 1, "description": "直接步骤"}]

    def test_steps_fallback_from_trace(self):
        """reasoning_result 缺 steps 时从 reasoning_trace 恢复（#7/#58 场景）。"""
        state = {
            "reasoning_result": {"answer": "x=2", "steps": []},
            "reasoning_trace": [
                {"step": "classification"},
                {"step": "reasoning",
                 "thinking": {"steps": [{"step_num": 1, "description": "兜底步骤A"},
                                        {"step_num": 2, "description": "兜底步骤B"}]}},
            ],
        }
        steps = _reasoning_steps(state)
        assert len(steps) == 2
        assert steps[0]["description"] == "兜底步骤A"

    def test_steps_empty_both(self):
        state = {"reasoning_result": {}, "reasoning_trace": []}
        assert _reasoning_steps(state) == []


class TestWithProofDetection:
    def test_chinese_proof(self):
        assert is_proof_question("证明：若 f 连续则 f 可积。") is True

    def test_english_prove_that(self):
        assert is_proof_question("Prove that the sequence converges.") is True

    def test_with_proof_computation(self):
        # #74/#7 实证：Find/Determine, with proof 必须识别为证明题
        assert is_proof_question(
            "Find, with proof, the smallest real number C with the following property: ...") is True
        assert is_proof_question(
            "Determine, with proof, the smallest possible size of S.") is True

    def test_plain_computation_not_proof(self):
        assert is_proof_question("Compute the integral of x^2 from 0 to 1.") is False


class TestOffTopicDetection:
    def test_off_topic_coin_game(self):
        # #28 实证：圆锥杯题答成硬币游戏
        state = {
            "problem": "A wealthy king has his blacksmith fashion him a large cup, "
                       "whose inside is a cone of height 9 inches and base diameter 6 inches.",
            "reasoning_result": {"answer": "A wins all coins with probability a/(a+b); "
                                           "expected rounds E[T] = ab"},
        }
        assert _reasoning_off_topic(state) is True

    def test_on_topic_normal(self):
        state = {
            "problem": "Find the cotangent of the minimum possible angle between "
                       "the vectors after 8 time steps.",
            "reasoning_result": {"answer": "The cotangent of the minimum angle is 8."},
        }
        assert _reasoning_off_topic(state) is False

    def test_empty_answer(self):
        assert _reasoning_off_topic({"problem": "x", "reasoning_result": {"answer": ""}}) is False


class TestPlayoffNeitherFallback:
    """V3.4：playoff NEITHER 保留候选兜底（#7 实证："无法确定"必 0 分）。"""

    def test_neither_keeps_candidate(self):
        from nodes.playoff_node import playoff_node

        class _Deps:
            def __init__(self):
                self.time_budget = None
                self.token_budget = None
                self.mcp_client = type("MC", (), {
                    "execute": lambda self, code, timeout: {
                        "success": True,
                        "stdout": "候选A核验: FAIL\n候选B核验: FAIL\nPLAYOFF_RESULT: NEITHER",
                        "stderr": ""}})()
                self.skills_loader = type("SL", (), {"get_validation_script": lambda self, c: ""})()
                self.logger = type("L", (), {"warning": lambda *a, **k: None,
                                             "info": lambda *a, **k: None})()
                self.client = type("C", (), {"chat": lambda self, **k: (
                    "```python\nprint('PLAYOFF_RESULT: NEITHER')\n```")})()

        state = {
            "problem": "short problem",
            "reasoning_result": {"answer": "候选A答案", "steps": []},
            "python_output": {"answer": "候选B答案", "evidence_status": "contradict"},
        }
        out = playoff_node(state, {"configurable": {"deps": _Deps()}})
        # NEITHER 分支必须保留候选作为兜底 validated_answer
        assert out.get("playoff_status") == "neither"
        assert out.get("validated_answer") in ("候选A答案", "候选B答案")
        assert out.get("answer_locked") is False


class TestSecondPythonCheck:
    """V3.4：Python 双验证前置条件（#4/#13/#46/#58/#91 实证）。"""

    def test_skips_without_playoff(self):
        state = {"python_output": {"answer": "42", "evidence_status": "support"}}
        assert _second_python_check(state, _Cfg()) is None

    def test_skips_when_playoff_decisive(self):
        state = {
            "playoff_trace": [{"status": "executed"}],
            "playoff_status": "decisive",
            "python_output": {"answer": "42", "evidence_status": "support"},
        }
        assert _second_python_check(state, _Cfg()) is None

    def test_skips_without_python_support(self):
        state = {
            "playoff_trace": [{"status": "executed"}],
            "playoff_status": "indecisive",
            "python_output": {"answer": "42", "evidence_status": "contradict"},
        }
        assert _second_python_check(state, _Cfg()) is None

    def test_skips_when_already_done(self):
        state = {
            "playoff_trace": [{"status": "executed"}],
            "playoff_status": "indecisive",
            "python_output": {"answer": "42", "evidence_status": "support"},
            "second_python_done": True,
        }
        assert _second_python_check(state, _Cfg()) is None


class TestLockedAnswerSteps:
    """V3.8：answer_locked 仲裁选 python 时计算题也必须附步骤（#3/#4/#13 实证）。"""

    def test_steps_attached_even_when_arbiter_chose_python(self):
        from utils.answer_formatter import attach_steps

        final = "最终答案：2**n*n + 1"
        problem = "Let S be a set ... (computation problem)"
        steps = [{"step_num": 1, "description": "验证 $f(T)=a^{|T|}$ 满足条件"},
                 {"step_num": 2, "description": "归纳推广到一般 n"}]
        out = attach_steps(final, problem, steps)
        assert "关键步骤" in out
        assert "最终答案：2**n*n + 1" in out  # verbatim 答案不被破坏
