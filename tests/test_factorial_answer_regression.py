"""回归：阶乘答案不得被"感叹号收尾"守卫清空。

背景
----
``is_junk`` 有一条"疑问/感叹收尾"守卫，本意是挡 ``好！``／``Really!`` 这类
思维流句子：

    # 疑问/感叹收尾：结论不以问句结束。
    if re.search(r"[？?！!]\\s*$", s):
        return True

它把 ``?`` 和 ``!`` 一并按"语句收尾"处理，于是**阶乘记号被当成感叹句**，
整条答案被清空。实测（修复前）：

    _distill_answer('n!')     -> ''        ← 答案凭空消失
    _reject_bad_answer('n!')  -> ''

``n!`` 是组合数学里最常见的答案形态之一，官方风格集 112 个参考答案里就有
一条正是 ``n!``（另有以 ``!`` 结尾的表达式）。清空等于该题必然失分。

修复：拆开两类结尾符号，``!`` 结尾先排除阶乘。判据只看 ``!`` 前一个字符——
``) ] }`` 或数字之后必是阶乘；**单个字母**之后是阶乘（``n!``），而英文单词
（``Really!``、``Yes!``）的 ``!`` 前面还有字母，仍判为感叹句；中文之后
（``好！``）不是阶乘。问句结尾的 ``?`` 行为完全不变。
"""

import pytest

from nodes.reason_step import _distill_answer
from utils.answer_sanitize import _is_factorial_tail, is_junk


# ---------- 修复目标：阶乘必须存活 ----------

@pytest.mark.parametrize("text", [
    "n!", "k!", "m!", "x!",
    "5!", "100!", "2n!", "n!m!",
    "(n+1)!", "(k-1)!", "x_{n}!", "a_{k}!",
    "\\frac{n!}{k!}",
])
def test_factorial_answers_are_not_junk(text):
    assert is_junk(text) is False


@pytest.mark.parametrize("text", ["n!", "k!", "5!", "(n+1)!"])
def test_factorial_answers_survive_distill(text):
    assert _distill_answer(text) == text


# ---------- 不得回归：感叹句仍须拦下 ----------

@pytest.mark.parametrize("text", [
    "Really!", "Yes!", "Not yet!", "Wait!", "Aha!", "好！", "这是错的！",
])
def test_exclamation_sentences_are_still_junk(text):
    assert is_junk(text) is True


# ---------- 不得回归：问句收尾行为不变 ----------

@pytest.mark.parametrize("text", [
    "Are there any other sequences?",
    "是否存在？",
    "n!?",
])
def test_question_tails_are_still_junk(text):
    assert is_junk(text) is True


def test_internal_question_mark_still_allowed():
    # 内嵌 '?' 的合法记号不受影响（原有注释里明确要保住的行为）。
    assert is_junk("P(X>1)") is False


# ---------- 判据本身 ----------

@pytest.mark.parametrize("text,expected", [
    ("n!", True), ("k!", True), ("5!", True), ("(n+1)!", True),
    ("x_{n}!", True), ("f(x)!", True),
    ("Really!", False), ("Yes!", False), ("好！", False), ("这是错的！", False),
    ("n", False), ("", False), ("!", False),
])
def test_factorial_tail_classifier(text, expected):
    assert _is_factorial_tail(text) is expected
