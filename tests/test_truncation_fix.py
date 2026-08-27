# -*- coding: utf-8 -*-
"""V2.5 截断修复测试（官方 truncated_count=203 根因）。"""
import unittest

from utils.llm_retry import _looks_truncated


class TestLooksTruncated(unittest.TestCase):
    """V2.5 修复：官方 finish_reason 不可见时，写满 max_tokens 即判截断。"""

    def test_strong_latex_unclosed(self):
        # 未闭合 LaTeX：不依赖写满判据
        self.assertTrue(_looks_truncated("答案是 $x=2", 16384))
        self.assertTrue(_looks_truncated("答案是 \\frac{1}{2", 16384))
        self.assertTrue(_looks_truncated("结论：\\alpha", 16384))

    def test_full_tokens_with_unfinished_tail(self):
        # V2.5 修复核心：输出 ≈ max_tokens（官方截断形态）且尾部未完成标点
        s = ("详细推导" * 4100) + "="  # 16400+ 字符，超 16384
        self.assertTrue(_looks_truncated(s, 16384))

    def test_full_tokens_clean_tail_not_truncated(self):
        # 写满但结尾干净（可能是正常完整答案）→ 不判截断（1.5 倍兜底）
        s = "a" * 16384
        self.assertFalse(_looks_truncated(s, 16384))

    def test_short_clean_not_truncated(self):
        self.assertFalse(_looks_truncated("答案是 42", 16384))

    def test_over_15x_truncated(self):
        # 超过 1.5 倍：无标点也判截断
        s = "a" * 30000
        self.assertTrue(_looks_truncated(s, 16384))


class TestTruncationConfig(unittest.TestCase):
    """V2.5 修复：续写轮次与 token 提升。"""

    def test_config_updated(self):
        from config import CONFIG
        self.assertEqual(CONFIG["truncation_continue_max_rounds"], 4)
        self.assertEqual(CONFIG["truncation_continue_tokens"], 4096)


class TestUnclosedFenceDetection(unittest.TestCase):
    """V2.5 修复：未闭合代码围栏 = 截断强信号（python_agent 压缩重试触发）。"""

    def _unclosed(self, resp):
        import re
        if not resp:
            return False
        unclosed = bool(re.search(r"```python[ \t]*\n[\s\S]*$", resp)) \
            and "```" not in resp.split("```python", 1)[-1]
        return unclosed

    def test_unclosed_fence(self):
        resp = "```python\nimport sympy as sp\nx = sp.Symbol('x')\nprint(sp.solve(x**2-4, x))"
        self.assertTrue(self._unclosed(resp))

    def test_closed_fence(self):
        resp = "```python\nimport sympy as sp\nprint(1)\n```"
        self.assertFalse(self._unclosed(resp))

    def test_no_fence(self):
        resp = "我无法确定答案。"
        self.assertFalse(self._unclosed(resp))


if __name__ == "__main__":
    unittest.main()
