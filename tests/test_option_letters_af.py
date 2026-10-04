"""回归：选项字母范围必须覆盖官方题库的六选项（A-F）。

背景
----
``normalize_objective_answer`` 曾把选项字母硬编码为 A-E。官方题库
``benchmarks/ugmath_hard26.jsonl`` 的 idx 9 / idx 21 都是 **A-F 六选项多选**
（MCM），于是：

* ``"F"``   -> ``""``      —— 单选 F 的整题答案被清空
* ``"ABF"`` -> ``""``      —— 多选含 F 时整题答案被清空
* ``"A、C、F"`` -> ``"A、C"`` —— 静默丢掉 F，答案不完整

答案被清空等于该题必然判 0，因此这里把边界锁死，防止再次收窄。
"""

from utils.question_profile import (
    normalize_objective_answer,
    objective_answer_is_usable,
)


def test_single_letter_f_is_kept():
    assert normalize_objective_answer("F", "choice") == "F"
    assert objective_answer_is_usable("F", "choice")


def test_connected_letters_containing_f():
    assert normalize_objective_answer("ABF", "choice") == "A、B、F"
    assert normalize_objective_answer("CDE", "choice") == "C、D、E"


def test_separated_letters_containing_f():
    assert normalize_objective_answer("A、C、F", "choice") == "A、C、F"
    assert normalize_objective_answer("A,B,F", "choice") == "A、B、F"


def test_full_six_option_selection():
    # 全部选项都正确时的连写形态，上限不足会被整串清空。
    assert normalize_objective_answer("ABCDEF", "choice") == "A、B、C、D、E、F"


def test_fullwidth_letters_beyond_e():
    assert normalize_objective_answer("Ｆ", "choice") == "F"
    assert normalize_objective_answer("ＡＢＦ", "choice") == "A、B、F"


def test_non_option_text_is_not_converted():
    # 放宽字母范围不得把普通答案文本误当选项串。
    assert normalize_objective_answer("x=42", "choice") == ""
    assert normalize_objective_answer("正确", "choice") == ""
