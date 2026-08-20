"""冲刺满分机制 M5/M6/M7 回归测试。"""
import unittest

from utils.answer_extractor import extract_answer_fallback_with_source
from utils.counting_guard import (
    code_has_enumeration, detect_counting, prompt_clause)


class TestCountingGuard(unittest.TestCase):
    def test_detect(self):
        self.assertTrue(detect_counting("有多少种不同的选法？"))
        self.assertTrue(detect_counting("求所有子集的个数。"))
        self.assertTrue(detect_counting("How many ways are there?"))
        self.assertFalse(detect_counting("求 f'(3) 的值"))
        self.assertFalse(detect_counting("求该级数的和"))

    def test_prompt_clause_only_for_counting(self):
        self.assertIn("枚举对照", prompt_clause("有多少种取法？"))
        self.assertEqual(prompt_clause("求导数"), "")

    def test_code_enumeration_check(self):
        self.assertTrue(code_has_enumeration(
            "for n in range(2,5):\n    count=0\nprint('最终答案:', 1)"))
        self.assertTrue(code_has_enumeration(
            "from itertools import combinations\nprint(list(combinations([1,2],2)))"))
        # 只写闭式、无循环 → 未对照
        self.assertFalse(code_has_enumeration(
            "from math import comb\nprint('最终答案:', comb(10,3))"))
        self.assertFalse(code_has_enumeration("print('最终答案:', 1120)"))


class TestOutputLayerFix(unittest.TestCase):
    def test_evidence_line_rejected(self):
        # 思维流中"Line 2: 依据：…故"不得被当成答案
        evil = "Thinking Process:\n1. Analyze...\nLine 2: `依据：当 z=1 时 |z|=1 且 z⁴−2z+1=0，故"
        ans, src = extract_answer_fallback_with_source(evil)
        self.assertEqual(ans, "")

    def test_dangling_line_rejected(self):
        # 话说一半的行（故/因为/则收尾）不是答案
        ans, _ = extract_answer_fallback_with_source("一些推导\n因此 x 收敛，故")
        self.assertEqual(ans, "")

    def test_normal_lines_kept(self):
        self.assertEqual(extract_answer_fallback_with_source(
            "推理\n最终答案：42")[0], "42")
        self.assertEqual(extract_answer_fallback_with_source(
            "分析\nx = 5")[0], "x = 5")


if __name__ == "__main__":
    unittest.main()
