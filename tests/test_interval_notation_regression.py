"""回归：数学区间/集合记号不得被 Python 字面量"人性化"改写。

背景
----
``ast.literal_eval("(70, 175)")`` 会成功，因为 **Python 元组语法与数学开区间记号
恰好重合**。于是 ``_humanize_python_literal`` 把参考答案 ``(70, 175)`` 改写成
``70，175``——括号消失、半角逗号变全角；``[0,1]`` 变成 ``0，1``。

仓库基准集里这种形态就是参考答案本身（``benchmarks/ugmath_hard26.jsonl`` 的
``answer_slots``：``(70, 175)``、``(1, 5, 6)``），改写等于把答案换成另一种字符串。

修复只对"全是数字且长度 ≥2 的括号序列"保留原形态；``_format_literal_value``
真正要处理的 Python 分支结构化结果（字典、字符串序列）保持不变。
"""

from utils.response_builder import _humanize_python_literal, format_answer_for_output


def test_interval_notation_survives_formatting():
    assert format_answer_for_output("(70, 175)", "computation") == "(70, 175)"
    assert format_answer_for_output("(1, 5, 6)", "computation") == "(1, 5, 6)"


def test_bracketed_numeric_sequence_keeps_halfwidth_commas():
    out = format_answer_for_output("[0,1]", "computation")
    assert "，" not in out, out
    assert out.startswith("[") and out.endswith("]"), out


def test_python_structures_are_still_humanised():
    assert _humanize_python_literal("{'positive_definite': True}") == "正定性：正定"
    assert _humanize_python_literal("['a','b']") == "a，b"


def test_single_element_tuple_is_not_treated_as_interval():
    # 长度 <2 不按区间保留；单元素元组的 Python 形态不该出现在答案里。
    assert _humanize_python_literal("(5,)") == "5"


def test_numeric_answers_are_untouched():
    assert format_answer_for_output("13.8196382007634", "computation") == "13.8196382007634"
    assert format_answer_for_output("2/3", "computation") == "2/3"
