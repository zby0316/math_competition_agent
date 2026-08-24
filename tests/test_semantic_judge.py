# -*- coding: utf-8 -*-
"""semantic_judge 确定性判定器测试（V3.2 判分对齐层）。

覆盖：≈ 数值容差、科学计数 unicode 上标、句子型答案数字通道、
符号等价（latex/表达式）、多字段答案、final 提取、False 收紧守则。
"""
import pytest

from utils.semantic_judge import (
    final_answer_text,
    judge,
    norm_segments,
    parse_approx,
)


class TestParseApprox:
    def test_plain(self):
        assert parse_approx("答案 ≈ 10.049875621（因 101 ≡ 1 mod 4）") == 10.049875621

    def test_unicode_superscript_times10(self):
        # ×10⁻⁴ 的 ⁻⁴ 是 unicode 上标，必须转 ASCII 后解析
        assert parse_approx("u(0.5,0.5) ≈ 3.16329777×10⁻⁴") == pytest.approx(3.16329777e-4)

    def test_ascii_times10(self):
        assert parse_approx("I ≈ 2.5×10^3") == pytest.approx(2500.0)

    def test_no_approx(self):
        assert parse_approx("最小割值 = 1600") is None


class TestNormSegments:
    def test_strip_dollar_and_prefix(self):
        assert norm_segments("最终答案：$x = 5$") == ["5"]

    def test_multi_part(self):
        segs = norm_segments("最小割值：$1600$；最优划分方案：$A=\\{1,2\\}$")
        joined = "|".join(segs)
        assert "1600" in joined and "1,2" in joined
        assert "$" not in joined

    def test_equals_take_right(self):
        assert norm_segments("u(0) = 14(sinh1-1)/cosh1") == ["14(sinh1-1)/cosh1"]


class TestFinalAnswerText:
    def test_prefix_extraction(self):
        assert final_answer_text("最终答案：$x=5$\n\n关键步骤：...") == "$x=5$"

    def test_no_prefix_keeps_head(self):
        # 无"最终答案"前缀：保留原文（提取不切分号；判分语义判定看整体）
        text = "径向弱解为 $u(r)=...$；球心值 $u(0)$ 待定"
        assert final_answer_text(text) == text

    def test_empty(self):
        assert final_answer_text("") == ""


class TestJudge:
    def test_sqrt101_expr(self):
        assert judge("sqrt(101)", "G(χ) = √101 ≈ 10.049875621（因 101 ≡ 1 mod 4）") is True

    def test_python_expr_equivalent(self):
        # sympy 表达式与期望符号等价（相对容差内）
        assert judge("-4*pi**2*(2 + exp(-3))/9",
                     "I = -4π²(2+e^{-3})/9 ≈ -8.991372209") is True

    def test_expr_with_dummy_var_wrong(self):
        # 含未消去积分变量 z 的表达式与定值不等价——不得判 True（False 或弃权均可，
        # 关键是不标记"判分疑似误杀"）
        r = judge("-4*pi**2*((-z**3 + 3*z + 2)*exp(z + 1)/9 - exp(3))*exp(-z - 4)/(-z**3 + 3*z + 2)",
                  "I = -4π²(2+e^{-3})/9 ≈ -8.991372209")
        assert r is not True

    def test_scientific_notation(self):
        assert judge("3.16329777e-4", "u(0.5,0.5) ≈ 3.16329777×10⁻⁴") is True

    def test_wrong_numeric_rejected(self):
        # 49/147456 ≈ 3.3230e-4 与 3.1633e-4 相对差 5% —— 必须 False（相对容差 1e-4）
        assert judge("49/147456", "u(0.5,0.5) ≈ 3.16329777×10⁻⁴") is False

    def test_sentence_answer_digit_channel(self):
        # 句子型答案：expected 的数字都出现在候选里
        assert judge("元素 6 在 $\\mathbb{Z}_{15}$ 中的阶为 5。", "5") is True

    def test_sentence_answer_wrong(self):
        assert judge("42", "G(χ) = √101 ≈ 10.049875621") is False

    def test_multi_field_answer(self):
        # Step 0 真实命中：最小割（v257b idx=8 判分误杀案例）
        fa = "最小割值：$1600$；最优划分方案：$A=\\{1,2,3,4\\},\\ B=\\{5,6,7,8,9,10\\}$（或 $A,B$ 互换）"
        exp = "最小割值 = 1600；最优划分 A={1,2,3,4}，B={5,6,7,8,9,10}（或互换）"
        assert judge(fa, exp) is True

    def test_free_variable_allowed_none(self):
        # 带自由变量 r 的完整解 vs 点值：无法判定时允许 None（不误判 False）
        fa = "径向弱解为 $u(r)=\\dfrac{A\\sinh r-14\\cosh r}{r}+7r$，$u(0)=A$，其中 $A=14\\dfrac{\\sinh 1-1}{\\cosh 1}$"
        exp = "u(0) = 14[1-(1+e^{-1})/cosh(1)] = 14(sinh1-1)/cosh1 ≈ 1.589558352"
        assert judge(fa, exp) in (True, None)

    def test_truncated_numeric_precision_boundary(self):
        # 特征值精度：6 位 vs 9 位，相对差 1.6e-5 < 1e-4 → True（判分宽容，不误杀）
        fa = "λ₁ = 12.024899, λ₂ = 41.676700, λ₃ = 91.016142"
        exp = "λ1 ≈ 12.025085597，λ2 ≈ 41.679854933，λ3 ≈ 91.032279091"
        assert judge(fa, exp) is True

    def test_placeholder_returns_none(self):
        assert judge("", "5") is None
        assert judge(None, "5") is None


class TestJudgeFalseTightening:
    """False 收紧守则：数字有交集时不得轻易判 False（避免假新错）。"""

    def test_partial_digit_overlap_not_false(self):
        # 候选含 expected 的部分数字但不完全 → 弃权 None 而非 False
        r = judge("[1, 1, 5, 5, 9, 9, 10, 16]",
                  "[1, 1, 5, 5, 5, 5, 9, 9, 10, 10, 16]（对应 6 的 11 个划分）")
        assert r is not False
