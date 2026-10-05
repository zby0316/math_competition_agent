"""回归：多行 display-math 答案不得被清空。

背景
----
``_distill_answer`` 的策略 1~6 都是**逐行**跑的，而 ``\\[`` 与 ``\\]`` 各占一行时，
单看每一行都是"括号不配平"（``\\[`` 有开无闭、``\\]`` 有闭无开），于是每个候选
都被残片检查否掉、**整个答案被清空**。实测（修复前）：

    '\\[\\n\\left(\\frac{n+1}{2}\\right)^2\\n\\]'  -> ''      ← 答案凭空消失
    '\\[\\n|S| \\ge 8\\n\\]'                      -> ''

而**同样内容的单行写法** ``\\[|S| \\ge 8\\]`` 正常存活——唯一的变量就是换行，
说明这是纯粹的解析缺陷，与数学无关。官方风格集 112 个参考答案里有 2 条正是
多行 display 形态。

修复：策略 0 ——整段被 display-math 定界符包住时先剥掉定界符（``\\[ … \\]`` /
``$$ … $$``），再交给后续策略。定界符是排版标记、不是答案内容。修复后单行与
多行形态行为一致。
"""

import pytest

from nodes.reason_step import _distill_answer


# ---------- 修复目标：多行 display 必须存活 ----------

@pytest.mark.parametrize("text,expected", [
    ("\\[\n\\left(\\frac{n+1}{2}\\right)^2\n\\]", "\\left(\\frac{n+1}{2}\\right)^2"),
    ("\\[\n|S| \\ge 8\n\\]", "|S| \\ge 8"),
    ("\\[\n x^2 + 1 \n\\]", "x^2 + 1"),
    ("\\[\n\\frac{\\pi \\log(2)}{8}\n\\]", "\\frac{\\pi \\log(2)}{8}"),
    ("$$\nn! \n$$", "n!"),
    ("\\[\n2^k\n\\]", "2^k"),
])
def test_multiline_display_math_survives(text, expected):
    assert _distill_answer(text) == expected


def test_multiline_display_with_boxed_still_extracts_inner():
    # boxed 策略优先：定界符剥掉后仍应取出 boxed 内容。
    assert _distill_answer("\\[\n\\boxed{2018}\n\\]") == "2018"


# ---------- 一致性：单行与多行结果相同 ----------

@pytest.mark.parametrize("single,multi", [
    (r"\[|S| \ge 8\]", "\\[\n|S| \\ge 8\n\\]"),
    (r"\[\boxed{2018}\]", "\\[\n\\boxed{2018}\n\\]"),
    (r"\[x^2 + 1\]", "\\[\n x^2 + 1 \n\\]"),
])
def test_single_and_multi_line_agree(single, multi):
    assert _distill_answer(single) == _distill_answer(multi)


# ---------- 不得回归：普通形态不受影响 ----------

@pytest.mark.parametrize("text,expected", [
    ("n!", "n!"),
    ("(2,0),(0,-2)", "(2,0),(0,-2)"),
    ("42", "42"),
    ("\\frac{1}{2}", "\\frac{1}{2}"),
    ("p \\leq 1/4", "p \\leq 1/4"),
    ("[final conclusion]", ""),          # 占位符仍须清空
])
def test_ordinary_answers_unchanged(text, expected):
    assert _distill_answer(text) == expected


def test_unbalanced_display_is_still_rejected():
    # 只有定界符没有内容 → 不得凭空造出答案。
    assert _distill_answer("\\[\n\\]") in ("", "\\]", "")
