# -*- coding: utf-8 -*-
"""V2 M4 答案形式对齐测试。

覆盖：单值/区间/判断/枚举形态识别 + 真实失分场景复现。
"""
import unittest

from utils.form_align import check_form_alignment, expected_form


class TestExpectedForm(unittest.TestCase):

    def test_single_value_signals(self):
        self.assertEqual(expected_form("求 x 的值"), "single")
        self.assertEqual(expected_form("计算定积分的结果"), "single")
        self.assertEqual(expected_form("求半长"), "single")
        self.assertEqual(expected_form("问该集合元素个数"), "single")
        self.assertEqual(expected_form("求 n 的最大值"), "single")

    def test_interval_signals(self):
        self.assertEqual(expected_form("求 x 的取值范围"), "interval")
        self.assertEqual(expected_form("求置信区间"), "interval")
        self.assertEqual(expected_form("求定义域"), "interval")

    def test_judge_signals(self):
        self.assertEqual(expected_form("判断是否成立"), "judge")
        self.assertEqual(expected_form("该结论对吗"), "judge")
        self.assertEqual(expected_form("是否存在这样的 n"), "judge")

    def test_enum_signals(self):
        self.assertEqual(expected_form("列出所有解"), "enum")
        self.assertEqual(expected_form("求 x 的所有取值"), "enum")

    def test_no_signal(self):
        self.assertEqual(expected_form("解方程"), "unknown")


class TestCheckFormAlignment(unittest.TestCase):

    def test_idx94_confidence_interval_scenario(self):
        """真实失分复现：问半长（单值），答区间 → 错配。"""
        problem = ("设 X~N(mu,1)，n=16，求 mu 的 95% 置信区间长度的一半"
                   "（即 z_{0.025}*1/sqrt(n) 的数值）")
        # 智能体答了完整区间
        r = check_form_alignment(problem, "置信区间：[2.01, 2.99]")
        self.assertFalse(r["aligned"])
        self.assertEqual(r["expected"], "single")
        # 答了单值 → 对齐
        r2 = check_form_alignment(problem, "0.49")
        self.assertTrue(r2["aligned"])

    def test_single_answer_matches(self):
        r = check_form_alignment("求 x 的值", "x = 5")
        self.assertTrue(r["aligned"])

    def test_interval_answer_matches(self):
        r = check_form_alignment("求 x 的取值范围", "[1, 3]")
        self.assertTrue(r["aligned"])
        r2 = check_form_alignment("求定义域", "(-inf, 0) ∪ (0, +inf)")
        self.assertTrue(r2["aligned"])

    def test_judge_answer_matches(self):
        r = check_form_alignment("判断该命题是否成立", "是")
        self.assertTrue(r["aligned"])
        r2 = check_form_alignment("判断该命题是否成立", "结论：成立")
        self.assertTrue(r2["aligned"])
        # 判断题答了数值 → 错配
        r3 = check_form_alignment("判断该命题是否成立", "5")
        self.assertFalse(r3["aligned"])

    def test_unknown_problem_passes(self):
        r = check_form_alignment("解方程", "x = 2")
        self.assertTrue(r["aligned"])


class TestObjectiveChoiceForm(unittest.TestCase):
    """方向 5（V2.4）：选择题格式对齐——官方可能精确匹配选项字母。"""

    def test_choice_expected_form(self):
        # 显式"选择题"字面
        self.assertEqual(expected_form("选择题：下列哪个函数是奇函数？"), "choice")
        # 选项标记 A. B. C.
        self.assertEqual(
            expected_form("下列正确的是\nA. x B. y C. z"), "choice")
        # 带空括号 + 选项 → 必须是 choice 不是 fill（关键歧义修复）
        self.assertEqual(
            expected_form("下列说法正确的是（  ）\nA. 甲 B. 乙 C. 丙 D. 丁"),
            "choice")
        # 单选/多选字面
        self.assertEqual(expected_form("单选：下列积分收敛的是？"), "choice")
        self.assertEqual(expected_form("多选：下列命题正确的有？"), "choice")

    def test_judge_with_options_is_choice(self):
        """'判断下列说法正确的是' + 选项 → choice 不是 judge（审查发现边界修复）。"""
        self.assertEqual(
            expected_form("判断下列说法正确的是（  ）\nA. 甲 B. 乙 C. 丙 D. 丁"),
            "choice")
        self.assertEqual(
            expected_form("判断下列命题正确的有（多选）\nA. 甲 B. 乙 C. 丙"),
            "choice")

    def test_choice_answer_letters_aligned(self):
        problem = "选择题：下列哪个函数是奇函数？\nA. y=x² B. y=x³ C. y=|x|"
        self.assertTrue(check_form_alignment(problem, "B")["aligned"])
        self.assertTrue(check_form_alignment(problem, "选B")["aligned"])
        self.assertTrue(check_form_alignment(problem, "B、D")["aligned"])
        self.assertTrue(check_form_alignment(problem, "答案：C")["aligned"])

    def test_choice_answer_with_explanation_misaligned(self):
        """选择题答了完整解释而非字母 → 触发重述。"""
        problem = "选择题：下列哪个函数是奇函数？\nA. y=x² B. y=x³ C. y=|x|"
        r = check_form_alignment(problem, "B. y=x³ 是奇函数，因为 f(-x)=-f(x)")
        self.assertFalse(r["aligned"])
        self.assertEqual(r["expected"], "choice")

    def test_choice_numeric_answer_misaligned(self):
        """选择题答数值（而非字母）→ 错配（精确匹配下失分）。"""
        problem = "选择题：x²=4 的解是？\nA. 1 B. 2 C. 3"
        r = check_form_alignment(problem, "x=2")
        self.assertFalse(r["aligned"])


class TestObjectiveJudgeForm(unittest.TestCase):
    """方向 5（V2.4）：判断题判定词覆盖（正确/错误 与 judge_confirm 口径一致）。"""

    def test_judge_positive_words(self):
        problem = "判断：函数 f(x)=x² 在 R 上单调递增是否正确？"
        for ans in ("正确", "错误", "不正确", "对", "错", "是", "否", "成立", "不成立",
                    "√", "×", "错误，因为 f(-1)=f(1)"):
            self.assertTrue(
                check_form_alignment(problem, ans)["aligned"],
                f"判定词应对齐: {ans!r}")

    def test_judge_non_word_misaligned(self):
        problem = "判断：函数 f(x)=x² 在 R 上单调递增是否正确？"
        r = check_form_alignment(problem, "x²在R上不单调")
        self.assertFalse(r["aligned"])


class TestObjectiveFillForm(unittest.TestCase):
    """方向 5（V2.4）：填空题格式对齐。"""

    def test_fill_expected_form(self):
        self.assertEqual(expected_form("填空：若 x²=4，则 x=____"), "fill")
        self.assertEqual(expected_form("若 x²=4，则 x=（  ）"), "fill")
        self.assertEqual(expected_form("空1 为____，空2 为____"), "fill")

    def test_fill_answer_aligned(self):
        problem = "填空：若 x²=4，则 x=____"
        self.assertTrue(check_form_alignment(problem, "±2")["aligned"])
        self.assertTrue(check_form_alignment(problem, "空1: ±2")["aligned"])
        self.assertTrue(check_form_alignment(problem, "x=±2")["aligned"])

    def test_fill_multiple_blanks_answer(self):
        problem = "填空：空1 为____，空2 为____"
        r = check_form_alignment(problem, "空1: 3；空2: -1")
        self.assertTrue(r["aligned"])


if __name__ == "__main__":
    unittest.main()
