"""回归：模型占位符不得穿过防线出厂。

背景
----
``_PLACEHOLDERS`` 是"这个答案是占位符"的**全等**词表（归一化会剥掉方括号、
下划线、连字符并转小写，因此一项同时覆盖 ``[x]``、``【x】``、``x_y`` 等写法）。
词表原先收了 ``answer`` 却没收 ``conclusion``、收了「结论」却没收「最终结论」，
于是 ``[final conclusion]`` 这类占位符一路穿过全部防线出厂。

真实案例：官方风格集 idx 32 的 ``final_response`` 就是 ``[final conclusion]``
（参考答案是 ``p \\leq 1/4``）。整题因占位符被判错——而这是纯工程缺陷，
模型只要正常产出答案就能得分。

安全底线
--------
判定是全等匹配，**任何含实际内容的答案都不会被误伤**。用官方风格集 112 个
参考答案与本地跑批的模型真实产出做过差分验证：新旧行为完全一致，零误伤。
"""

import pytest

from utils.cot_trim import is_placeholder_answer


# ---------- 修复目标：这些占位符必须被识别 ----------

@pytest.mark.parametrize("text", [
    "[final conclusion]",          # ← idx 32 实际出厂的形态
    "[Final Conclusion]",
    "final conclusion",
    "conclusion",
    "[final result]",
    "[insert answer]",
    "[your answer]",
    "[answer here]",
    "TBD",
    "tbd",
    "xxx",
    "最终结论",
    "[最终结论]",
    "答案见上",
    "见上",
    "如上",
    "略",
    "略去",
    "省略",
    "待定",
])
def test_placeholder_variants_are_detected(text):
    assert is_placeholder_answer(text) is True


# ---------- 不得回归：原本就拦住的 ----------

@pytest.mark.parametrize("text", [
    "[结论]", "[答案]", "[final answer]", "[描述]", "[具体公式]",
    "none", "null", "N/A", "placeholder", "todo",
    "同上", "无法确定", "未知", "...",
])
def test_existing_placeholders_still_detected(text):
    assert is_placeholder_answer(text) is True


def test_empty_and_whitespace_are_placeholders():
    assert is_placeholder_answer("") is True
    assert is_placeholder_answer("   ") is True


# ---------- 安全底线：真实答案一律不得被误判 ----------

@pytest.mark.parametrize("text", [
    "72",
    "p \\leq 1/4",
    "(2,0),(0,-2)",
    "\\frac{\\pi \\log(2)}{8}",
    "2^k",
    "n!",
    "1/3 < c \\leq 1",
    "c\\in\\left(\\dfrac{1}{3},\\,1\\right]",
    "f(x) = 0 \\ \\ \\forall x \\in \\mathbb{R}",
    "There exists a positive integer n for which g(n) > n^{0.999n}.",
    "Yes, such a set exists",
    "No",
    # 含"结论""答案"等词、但承载真实内容的答案，绝不能被全等词表误伤
    "结论：x = 3",
    "答案为 42",
    "综上，存在无穷多个满足条件的 n",
    "略大于 1 的最小整数是 2",
    "待定系数 a = 3, b = -1",
])
def test_real_answers_survive(text):
    assert is_placeholder_answer(text) is False
