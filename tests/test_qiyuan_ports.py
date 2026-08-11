"""启元移植机制回归测试：判断题双向确认 + 推导矛盾自检。"""
import unittest

from utils.deps import MockClient
from utils.derivation_conflict import detect_derivation_conflict
from utils.judge_confirm import (
    is_judge_problem, judge_polarity, normalize_judge_word, should_confirm,
)


class _Deps:
    def __init__(self, responses):
        self.client = MockClient(responses)
        self.token_budget = None
        self.time_budget = None
        import logging
        self.logger = logging.getLogger("test")


class TestJudgeConfirmUtils(unittest.TestCase):
    def test_problem_detection(self):
        self.assertTrue(is_judge_problem("该命题是否成立？"))
        self.assertTrue(is_judge_problem("能否断言 f 连续？"))
        self.assertFalse(is_judge_problem("求 f'(3) 的值"))

    def test_word_normalize(self):
        self.assertEqual(normalize_judge_word("最终答案：是"), "是")
        self.assertEqual(normalize_judge_word("不成立"), "不成立")
        self.assertEqual(normalize_judge_word("答案：错误。"), "错误")
        self.assertEqual(normalize_judge_word("x=3"), "")
        # "不成立"不得被"成立"截胡
        self.assertEqual(judge_polarity(normalize_judge_word("不成立")), "否")

    def test_should_confirm(self):
        self.assertTrue(should_confirm("该级数是否收敛？", "是"))
        self.assertFalse(should_confirm("该级数是否收敛？", "收敛于 1"))  # 非判断词
        self.assertFalse(should_confirm("求和", "是"))  # 非判断题

    def test_run_confirm_consistent(self):
        from utils.judge_confirm import run_judge_confirmation
        deps = _Deps(["该级数通项趋于 0 且单调递减，由莱布尼茨判别法收敛。\n结论:是"])
        out = run_judge_confirmation("该级数是否收敛？", "是", deps)
        self.assertEqual(out["action"], "confirm")
        self.assertEqual(out["final_word"], "是")

    def test_run_confirm_reverse_with_resolve(self):
        from utils.judge_confirm import run_judge_confirmation
        deps = _Deps([
            "反例：n=1 时不成立。\n结论:否",          # 自证反向
            "逐步推导……\n答案:否",                      # 温度0重解第三票
        ])
        out = run_judge_confirmation(
            "该命题是否成立？", "是", deps,
            main_prompt_builder=lambda: "重解提示")
        self.assertEqual(out["action"], "reverse")
        self.assertEqual(out["final_word"], "否")

    def test_run_confirm_failure_keeps_answer(self):
        class Raiser:
            def chat(self, *a, **kw):
                raise TimeoutError("x")
        deps = _Deps([])
        deps.client = Raiser()
        from utils.judge_confirm import run_judge_confirmation
        out = run_judge_confirmation("该命题是否成立？", "是", deps)
        self.assertEqual(out["action"], "keep")
        self.assertEqual(out["final_word"], "是")


class TestDerivationConflict(unittest.TestCase):
    def test_detects_conflict(self):
        text = "步骤1：代入得 x=3。\n步骤4：因此 x=5。"
        self.assertIn("x 同时取值", detect_derivation_conflict(text))

    def test_numeric_normalization_no_false_positive(self):
        # x=3 与 x=3.0 与 x=3/1 数值等价，不判矛盾
        text = "步骤1：代入得 x=3。\n步骤4：因此 x=3.0。"
        self.assertIsNone(detect_derivation_conflict(text))

    def test_excludes_hypothesis_and_inequality(self):
        self.assertIsNone(detect_derivation_conflict("若 x=3，则……；假设 x=5 讨论"))
        self.assertIsNone(detect_derivation_conflict("解得 x≠3；于是 x=5"))

    def test_excludes_multisolution_and_enumeration(self):
        self.assertIsNone(detect_derivation_conflict("解得 x=2 或 x=-2"))
        self.assertIsNone(detect_derivation_conflict("枚举：x=1, x=2, x=3"))

    def test_empty_and_clean(self):
        self.assertIsNone(detect_derivation_conflict(""))
        self.assertIsNone(detect_derivation_conflict("步骤1：得 x=3。步骤2：所以 y=4。"))


if __name__ == "__main__":
    unittest.main()
