# -*- coding: utf-8 -*-
"""form_align re.DOTALL 修复回归测试（2026-08-23 审计发现）。

历史缺陷：coordinator_node._form_align_reframe 用 `re.DOTALL` 但模块内
只有 `import re as _re` → NameError 被 except 吞掉 → 重述恒返回 "" →
enable_form_align 形式对齐功能运行时 100% 失效。本测试锁定修复。
"""
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")

from nodes.coordinator_node import _form_align_reframe  # noqa: E402


class TestFormAlignReframe(unittest.TestCase):
    def _deps(self):
        deps = MagicMock()
        deps.client = MagicMock()
        deps.client.chat = MagicMock(
            return_value="最终答案：$\\frac{3}{5}$\n计算步骤略。")
        deps.time_budget = MagicMock()
        deps.time_budget.remaining_hard.return_value = 1000
        deps.time_budget.remaining.return_value = 900
        deps.time_budget.can_afford.return_value = True
        deps.time_budget.expired.return_value = False
        deps.time_budget.llm_calls_exhausted.return_value = False
        deps.token_budget = MagicMock()
        deps.logger = MagicMock()
        return deps

    def test_reframe_extracts_answer_after_prefix(self):
        """修复前：re.DOTALL NameError → 返回 ""；修复后应提取 "3/5"。"""
        deps = self._deps()
        out = _form_align_reframe(
            deps.client, deps, "求概率", "0.6", "fraction")
        self.assertEqual(out, "$\\frac{3}{5}$")

    def test_reframe_no_prefix_keeps_text(self):
        deps = self._deps()
        deps.client.chat = MagicMock(return_value="$\\frac{1}{2}$")
        out = _form_align_reframe(deps.client, deps, "题", "0.5", "fraction")
        self.assertEqual(out, "$\\frac{1}{2}$")

    def test_reframe_failure_returns_empty(self):
        deps = self._deps()
        deps.client.chat = MagicMock(side_effect=RuntimeError("boom"))
        out = _form_align_reframe(deps.client, deps, "题", "x", "choice")
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
