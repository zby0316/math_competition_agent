"""回归：LaTeX 命令不得触发"不安全表达式"守卫（\\lambda 误判）。

背景
----
``contains_unsafe_expr_text`` 用关键字表拦 sympify 的隐式 eval 风险
（实测 ``sympify("open('/etc/hosts')")`` 会真实打开文件）。但关键字表里的
``lambda`` 会命中 LaTeX 的 ``\\lambda``：

    contains_unsafe_expr_text('\\lambda \\ge 1')   -> True   （修复前）
    contains_unsafe_expr_text('2\\lambda+1')       -> True
    contains_unsafe_expr_text('x = \\lambda')      -> True

后果：凡答案含独立 λ 的题，``answer_compare._try_parse_expr`` 直接放弃符号比较
（``response_builder`` 也跳过 sympify 归一），只能退到字符串相似度——推理侧与
Python 侧明明等价却判不出，容易落到 uncertain 分支。

λ 出现在特征值、谱理论、Lagrange 乘子、特征方程里，是数学里最高频的符号之一。
官方风格集 112 个参考答案里就有一条是 ``\\lambda \\ge \\frac{1}{n-1}`` 被误判。

修复：在负向后顾里加入反斜杠——LaTeX 命令（``\\lambda``/``\\open``/``\\eval``）
不再命中；反斜杠在 Python 中不能作为标识符起始，因此**不削弱守卫**，真实载荷
（``open(...)``、``__import__``、``exec(...)``、``(lambda: 1)()``）仍被拦住。
"""

import pytest

from utils.cot_trim import contains_unsafe_expr_text


# ---------- 修复目标：LaTeX 数学记号必须放行 ----------

@pytest.mark.parametrize("text", [
    r"\lambda",
    r"\lambda \ge 1",
    r"\lambda \ge \frac{1}{n-1}",
    r"2\lambda+1",
    r"x = \lambda",
    r"特征值 \lambda = 3",
    r"\mu, \nu, \lambda",
    r"\lambda_{max}",
    r"\Lambda",
    r"\lambda_1 = 2",
])
def test_latex_lambda_is_safe(text):
    assert contains_unsafe_expr_text(text) is False


@pytest.mark.parametrize("text", [
    r"\open", r"\eval", r"\vars", r"\input", r"\compile", r"\exec",
])
def test_other_latex_commands_are_safe(text):
    # 这些不是真实 LaTeX 命令，但同样不该被当 Python 关键字拦下——
    # 反斜杠起始在 Python 里不可能构成标识符。
    assert contains_unsafe_expr_text(text) is False


# ---------- 安全底线：真实载荷必须仍然被拦 ----------

@pytest.mark.parametrize("text", [
    "open('/etc/hosts')",
    "__import__('os')",
    "exec('print(1)')",
    "eval('1+1')",
    "(lambda: 1)()",
    "getattr(x, 'y')",
    "setattr(x, 'y', 1)",
    "vars()",
    "globals()",
    "locals()",
    "compile('1','','eval')",
    "input()",
    "breakpoint()",
    "chr(65)",
    "ord('a')",
    "import os",
    "'quoted'",
    "`backtick`",
    "a.__class__",
    "x\\nopen('/etc/hosts')",
])
def test_real_payloads_are_still_unsafe(text):
    assert contains_unsafe_expr_text(text) is True


def test_plain_math_answers_are_safe():
    for s in ["72", r"\frac{1}{2}", "x^2+1", "n!", "(2,0),(0,-2)", r"p \leq 1/4"]:
        assert contains_unsafe_expr_text(s) is False
