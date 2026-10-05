"""回归：多解答案（元组序列）不得被拍平成数字串。

背景
----
``_is_numeric_sequence_literal`` 只认「一层全是标量数字」的括号序列，用来把
``(70, 175)``、``[0,1]``、``(1, 5, 6)`` 这类**数学记号**从 ``_humanize_python_literal``
的人性化分支里豁免出来。但「全部解」是**嵌套**结构——每个内层元组是一组解——
旧判据对 ``[(0, -2), (2, 0)]`` 返回 False，于是走了拍平分支。实测（修复前）：

    '[(0, -2), (2, 0)]'            -> '0，-2，2，0'      ← 解对结构全丢
    '[(1, 2), (3, 4), (5, 6)]'     -> '1，2，3，4，5，6'  ← 三组解糊成一串
    '[(0,0)]'                      -> '0，0'             ← 单组解同样被拍平
    '[[1, 2], [3, 4]]'             -> '1，2，3，4'

真实案例：官方风格集 idx 3（求 $x^3-y^3=2xy+8$ 的全部整数解）。trace 里推理侧
给出 ``$(2, 0)$ 和 $(0, -2)$``、Python 侧给出 ``[(0, -2), (2, 0)]``、验证状态
``validated_answer`` **已经是正确的** ``[(0, -2), (2, 0)]``——但交付时被打散成
``0，-2，2，0``，判分器再也认不出参考答案 ``(2,0),(0,-2)``。

"求所有解 / 所有 $(x,y)$ / 所有点"在竞赛题里极常见，因此这是系统性失分点。

修复：判据扩展到嵌套数字序列（含外层只有一个元素的情形）。
``[5]``、``[0]``、``['a']`` 这类单元素标量序列仍按原样拍平——单元素序列本身
不是数学记号。
"""

from utils.response_builder import format_answer_for_output as fmt


def _c(s: str) -> str:
    return fmt(s, "computation")


# ---------- 修复目标：多解答案保留结构 ----------

def test_solution_pair_list_is_preserved():
    assert _c("[(0, -2), (2, 0)]") == "[(0, -2), (2, 0)]"


def test_three_solutions_are_preserved():
    assert _c("[(1, 2), (3, 4), (5, 6)]") == "[(1, 2), (3, 4), (5, 6)]"


def test_single_solution_list_is_preserved():
    # 外层只有一个元素、内层是一组解——同样是数学记号，不能拍平成 "0，0"。
    assert _c("[(0,0)]") == "[(0, 0)]"
    assert _c("[[1, 2]]") == "[[1, 2]]"


def test_list_of_lists_is_preserved():
    assert _c("[[1, 2], [3, 4]]") == "[[1, 2], [3, 4]]"


def test_bare_nested_tuple_is_rerendered_with_structure():
    # 裸 "(1,2),(3,4)" 不是合法 Python 字面量吗？ast 会解析成嵌套元组，
    # 于是按结构重新渲染——关键是不再退化成 "1，2，3，4"。
    assert _c("(1,2),(3,4)") == "((1, 2), (3, 4))"


# ---------- 原有保护：不得回归 ----------

def test_single_layer_numeric_sequences_stay_untouched():
    assert _c("(70, 175)") == "(70, 175)"
    assert _c("(1, 5, 6)") == "(1, 5, 6)"
    assert _c("[1, 2, 3]") == "[1, 2, 3]"
    assert _c("(1, 2)") == "(1, 2)"


def test_single_element_scalar_is_still_flattened():
    # 单元素序列不是数学记号："[5]" 拍平成 "5" 是有意行为。
    assert _c("[5]") == "5"
    assert _c("[0]") == "0"
    assert _c("['a']") == "a"


def test_dict_branch_still_humanized():
    assert _c("{'positive_definite': True}") == "正定性：正定"
    assert _c("{'a': 1, 'b': 2}") == "a：1；b：2"


def test_string_sequence_still_humanized():
    assert _c("['a', 'b']") == "a，b"


def test_tuple_sequence_with_strings_still_humanized():
    # 内层含字符串 → 不是纯数学记号，保持人性化分支。
    assert _c("[('a', 1), ('b', 2)]") == "a，1，b，2"


def test_scalars_and_latex_untouched():
    assert _c("42") == "42"
    assert _c("\\frac{1}{2}") == "\\frac{1}{2}"
