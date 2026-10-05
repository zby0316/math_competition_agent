"""回归：编号多问答案不得被判为"思维流碎片"。

背景
----
``is_junk`` 有一条针对"裸数值后接英文散句"的守卫，用来挡
``"-177. Strict. So equality only when all equal."`` 这类解题现场的思维流：

    if re.match(r"^[-+]?\\d+(?:\\.\\d+)?\\s*[.,;]\\s+[A-Za-z]", s) \\
            and not re.search(r"\\\\boxed|=", s):
        return True

但**编号列表同样以"数字. 英文"开头**，多问答案的常见写法是

    1. When t→+∞, M(t)→+∞. Therefore, N(t) also diverges to +∞.
    2. For k≠0, p_k(t,v) decays, and ...

它不含 ``=``（纯文字结论）时会被整条判为思维流。实测：官方风格集 112 个参考
答案里 idx 38 正是这种两问编号答案，被判成 junk——等于该题必然失分。

修复：增加"不存在第二个编号项"的条件。裸值散句不会有 ``\\n2.``。中文编号、
括号编号、圆括号编号原本就不匹配该正则，行为不变。
"""

import pytest

from utils.answer_sanitize import is_junk


# ---------- 修复目标：编号多问答案不是 junk ----------

@pytest.mark.parametrize("text", [
    "1. When t→+∞, M(t)→+∞. Therefore, N(t) also diverges to +∞.\n"
    "2. For k≠0, p_k(t,v) decays, and p(t,x,v) is approximately ...",
    "1. x = 1\n2. y = 2",
    "1. 第一个结论成立\n2. 第二个结论不成立",
    "1) 结论一\n2) 结论二",
    "(1) 结论一\n(2) 结论二",
    "1. First part holds\n2. Second part fails\n3. Third part is open",
])
def test_numbered_multi_part_answers_are_not_junk(text):
    assert is_junk(text) is False


# ---------- 不得回归：裸值 + 英文散句仍是 junk ----------

@pytest.mark.parametrize("text", [
    "-177. Strict. So equality only when all equal.",
    "0.5. This works for all cases.",
    "42. Yes indeed.",
    "3.25. Strict inequality everywhere.",
])
def test_bare_number_plus_prose_is_still_junk(text):
    assert is_junk(text) is True


# ---------- 其他既有行为不受影响 ----------

@pytest.mark.parametrize("text,expected", [
    ("n!", False),
    ("72", False),
    ("(2,0),(0,-2)", False),
    ("好！", True),
    ("Really!", True),
    ("Are there any other sequences?", True),
    ("P(X>1)", False),
    ("", True),
])
def test_other_guards_unchanged(text, expected):
    assert is_junk(text) is expected


def test_single_number_without_prose_is_not_junk():
    # 只有数值本身不是"思维流"——它是合法答案。
    for s in ["42", "-177", "0.5", "1/2"]:
        assert is_junk(s) is False
