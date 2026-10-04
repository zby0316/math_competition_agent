"""回归：多栏裸值答案不得只剩最后一个；负号不得被当列表符号剥掉。

背景
----
``_distill_answer`` 的兜底策略「短节（≤80 字符）→ 取最后一行」会把逐行罗列的多栏
答案压成一栏。实测（修复前）：

    '70\\n175'           -> '175'
    'A\\nB\\nC'          -> 'C'
    '1\\n2\\n3\\n4\\n5\\n6' -> '6'      ← 六栏答案只剩一栏
    '上界: 70\\n下界: 175' -> '下界: 175'

（``'13.5\\n19.4'`` 侥幸存活，是因为 ``19.4`` 里的 ``.`` 被 ``_ENUM_ITEM_RE`` 当成
了枚举编号——纯属巧合。）

修复新增策略 2.7：每一行都是「裸值」时逐行拼接；叙述式短节不受影响。

顺带修掉一个潜伏缺陷：``_strip_bullet`` 原本用 ``lstrip("-*•①②③④⑤ ")``，会把
``-3`` 剥成 ``3``——**负号丢失等于答案错**。现在只有 ``-`` 后跟空白才算列表标记。
"""

from nodes.reason_step import _parse_reasoning_output, _strip_bullet


def _answer(text: str) -> str:
    return _parse_reasoning_output(f"## 最终答案\n{text}", "computation")["answer"]


def test_bare_multi_value_lines_are_joined():
    assert _answer("70\n175") == "70；175"
    assert _answer("A\nB\nC") == "A；B；C"


def test_six_slot_answer_keeps_all_slots():
    assert _answer("1\n2\n3\n4\n5\n6") == "1；2；3；4；5；6"


def test_narrative_short_section_keeps_last_line_behaviour():
    # 叙述式短节不得被拼接，保持"取最后一行"的原有行为。
    assert _answer("由上式可知\n答案：42") == "答案：42"


def test_minus_sign_is_not_stripped_as_bullet():
    assert _strip_bullet("-3") == "-3"
    assert _strip_bullet("-0.5") == "-0.5"
    assert _answer("-3\n7\n2") == "-3；7；2"


def test_bullet_markers_are_still_stripped():
    assert _strip_bullet("- 3") == "3"
    assert _strip_bullet("* 42") == "42"
    assert _strip_bullet("• 7") == "7"
