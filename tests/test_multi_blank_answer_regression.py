"""回归：多空填空答案不得只剩最后一个空位。

背景
----
``_distill_answer`` 的兜底策略「短节（≤80 字符）→ 取最后一行」对多空填空题是
破坏性的，因为 ``_ENUM_ITEM_RE`` 既不认括号编号 ``(1)``、也不认 ``空N:`` 前缀，
多空答案就直接落到那条策略上。

实测（修复前）：

    空1: 移动平均法\\n空2: 时间序列分解法  ->  '空2: 时间序列分解法'（丢空1）
    (1) 移动平均法\\n(2) 时间序列分解法    ->  '(2) 时间序列分解法'（丢第 1 空）
    空1: A\\n空2: B\\n空3: C              ->  '空3: C'（三空只剩一空）

判分器按空位逐项比对时，这等于整题作废；只有恰好写成同一行、用「；」分隔才侥幸
完整。修复新增策略 2.6：逐空标签行整体保留（用既有的 ``_join_parts`` 以「；」连接）。
"""

from nodes.reason_step import _parse_reasoning_output


def _answer(text: str) -> str:
    return _parse_reasoning_output(f"## 最终答案\n{text}", "fill")["answer"]


def test_two_blank_lines_are_all_kept():
    out = _answer("空1: 移动平均法\n空2: 时间序列分解法")
    assert "移动平均法" in out
    assert "时间序列分解法" in out


def test_parenthesised_numbering_is_kept():
    out = _answer("(1) 移动平均法\n(2) 时间序列分解法")
    assert "移动平均法" in out
    assert "时间序列分解法" in out


def test_fullwidth_parenthesised_numbering_is_kept():
    out = _answer("（1）移动平均法\n（2）时间序列分解法")
    assert "移动平均法" in out
    assert "时间序列分解法" in out


def test_three_blanks_are_all_kept():
    out = _answer("空1: A\n空2: B\n空3: C")
    for token in ("A", "B", "C"):
        assert token in out


def test_single_blank_is_unaffected():
    assert _answer("空1: 移动平均法") == "空1: 移动平均法"
