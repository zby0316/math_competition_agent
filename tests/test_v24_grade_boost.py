# -*- coding: utf-8 -*-
"""V2.4 提分机制单元测试：🅰数值比对兜底 + 🅱统计口径 + 🅲执行失败修复。

覆盖（对应 v24_112 评测 2 道错题的根因修复）：
- idx=85 留数符号：Python 验证内部自洽 PASS 但符号与候选相反 → 必须被
  确定性数值比对降级为 contradict（不能出厂 support）。
- idx=92 方差口径：统计题未明确口径时提示词要求按总体方差（÷n）作答。
- 代码执行失败（语法错误）应获得一次独立 exec-fix 重试，而不是裸出厂。
"""
import unittest

import sympy as sp

from utils.numeric_crosscheck import numeric_crosscheck, _extract_scalar_expression


class TestNumericCrosscheck(unittest.TestCase):
    """🅰 确定性数值比对兜底（idx=85 根因）。"""

    def test_residue_sign_mismatch_detected(self):
        """idx=85 场景：候选正号错误 vs python 负号正确 → mismatch。"""
        candidate = (
            "\\operatorname{Res}(f,e^{i\\pi/4})="
            "\\frac{1+i}{4\\sqrt{2}}=\\frac{\\sqrt{2}}{8}+i\\frac{\\sqrt{2}}{8}"
        )
        python_answer = "-(-1)**(1/4)/4"
        r = numeric_crosscheck(candidate, python_answer)
        self.assertEqual(r["status"], "mismatch",
                         f"应判定 mismatch，实际 {r['status']}: {r['reason']}")
        self.assertIn("数值不一致", r["reason"])

    def test_equivalent_forms_match(self):
        """等价表达（不同写法同数值）→ match，不误伤。"""
        r = numeric_crosscheck("\\frac{\\sqrt{2}}{4}", "sqrt(2)/4")
        self.assertEqual(r["status"], "match", r["reason"])

    def test_equal_scalars_match(self):
        r = numeric_crosscheck("x=5", "5")
        self.assertEqual(r["status"], "match", r["reason"])

    def test_different_scalars_mismatch(self):
        r = numeric_crosscheck("均值 5；方差 5", "6")
        self.assertEqual(r["status"], "mismatch", r["reason"])

    def test_missing_python_answer_inconclusive(self):
        """python 无答案 → inconclusive（不误降级，idx=92 前 python 失败场景）。"""
        r = numeric_crosscheck("样本方差：$s^2=\\dfrac{20}{3}$", "")
        self.assertEqual(r["status"], "inconclusive", r["reason"])

    def test_non_numeric_inconclusive(self):
        """含自由符号/无法解析 → inconclusive（保守不误伤）。"""
        r = numeric_crosscheck("证明：f(x) 连续", "5")
        self.assertEqual(r["status"], "inconclusive", r["reason"])

    def test_complex_equivalent_float_check(self):
        """符号形式不同但数值相同（浮点交叉验证）→ match。"""
        r = numeric_crosscheck("\\frac{1+i}{4\\sqrt{2}}", "(1+I)/(4*sqrt(2))")
        self.assertEqual(r["status"], "match", r["reason"])

    def test_extract_latex_frac_nested_sqrt(self):
        """\\frac{\\sqrt{2}}{8} 嵌套 sqrt 提取为复数表达式。"""
        expr, ok = _extract_scalar_expression("\\frac{\\sqrt{2}}{8}+i\\frac{\\sqrt{2}}{8}")
        self.assertTrue(ok)
        self.assertAlmostEqual(complex(sp.N(expr, 30)).real, 0.17677669529663687, places=12)
        self.assertAlmostEqual(complex(sp.N(expr, 30)).imag, 0.17677669529663687, places=12)


class TestPromptStatConvention(unittest.TestCase):
    """🅱 统计口径提示词（idx=92 根因）。"""

    def test_reasoning_prompt_has_variance_convention(self):
        from utils.prompt_templates import REASONING_PROMPT
        self.assertIn("统计口径对齐", REASONING_PROMPT)
        self.assertIn("除以 n", REASONING_PROMPT)
        self.assertIn("n-1", REASONING_PROMPT)

    def test_python_prompt_unchanged(self):
        """PYTHON_PROMPT 仍要求独立比较候选（原有契约未破坏）。"""
        from utils.prompt_templates import PYTHON_PROMPT
        self.assertIn("待核验候选", PYTHON_PROMPT)


class TestPythonExecFixLogic(unittest.TestCase):
    """🅲 执行失败修复重试（idx=92 代码生成语法错误根因）。"""

    def test_exec_fix_flags_exist(self):
        """python_agent_node 引入 exec_fix_used / exec_fix_pending 标志。"""
        import inspect
        import nodes.python_agent_node as mod
        src = inspect.getsource(mod)
        self.assertIn("exec_fix_used = False", src)
        self.assertIn("exec_fix_pending = False", src)
        self.assertIn("exec_fix_retry", src)

    def test_finalize_calls_numeric_crosscheck(self):
        """finalize 中接入数值比对兜底（mismatch → contradict）。"""
        import inspect
        import nodes.python_agent_node as mod
        src = inspect.getsource(mod)
        self.assertIn("from utils.numeric_crosscheck import numeric_crosscheck", src)
        self.assertIn('enriched["evidence_status"] = "contradict"', src)

    def test_module_imports_clean(self):
        """改动后模块可正常导入（无语法/依赖错误）。"""
        import nodes.python_agent_node  # noqa: F401
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
