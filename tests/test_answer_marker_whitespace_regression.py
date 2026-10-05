"""回归：``最终答案`` 标记与冒号之间的空白必须容忍。

背景
----
``_extract_answer_from_output`` 用 ``r"最终答案[：:][ \\t]*"`` 定位答案标记，
要求"最终答案"后**紧跟**冒号。模型偶尔写成带空格的形态：

    验证状态: PASS
    最终答案 ： 42        ← 冒号前有一个空格 → 旧正则不匹配 → 返回 None

返回 None 的语义是"程序没走到自己的结论"，调用方会据此**重试一次完整调用**
（白烧时间与额度），而不是解析出已经打印好的答案。``utils/extractors.py``
与 ``nodes/assemble_step.py`` 有同样的正则，一并加固。

修正是纯放宽：只允许标记与冒号之间出现空白，不会让任何原本匹配的形态失效。
``答案为 42``（完全无标记）是执行器的刻意设计（提示词要求以 ``最终答案:`` 收尾），
由 ``utils/extractors.extract_final_answer`` 在另一条通道覆盖。
"""

import pytest

from exec_server.executor import _extract_answer_from_output
from utils.extractors import extract_final_answer


@pytest.mark.parametrize("stdout,expected", [
    ("验证状态: PASS\n最终答案: 42", "42"),
    ("验证状态: PASS\n最终答案:42", "42"),
    ("验证状态: PASS\n最终答案 ： 42", "42"),          # ← 修复目标
    ("验证状态: PASS\n最终答案  :  42", "42"),         # ← 修复目标
    ("验证状态：PASS\n最终答案：42", "42"),
    ("验证状态: PASS\n最终答案: (1/3, 1]", "(1/3, 1]"),
    ("验证状态: PASS\n最终答案: n!", "n!"),
    ("验证状态: PASS\n最终答案: [(0, -2), (2, 0)]", "[(0, -2), (2, 0)]"),
    ("验证状态: PASS\n最终答案: [[1, 2], [3, 4]]", "[[1, 2], [3, 4]]"),
    ("验证状态: PASS\n最终答案: \\lambda \\ge 1", "\\lambda \\ge 1"),
])
def test_executor_tolerates_whitespace_before_colon(stdout, expected):
    assert _extract_answer_from_output(stdout) == expected


def test_executor_still_requires_the_marker():
    # 没有"最终答案:"标记时返回 None 是刻意设计（程序没走到结论 → 让调用方重试）。
    # 旧回退会把中间计算日志当答案送进仲裁，因此这里必须保持返回 None。
    assert _extract_answer_from_output("验证状态: PASS\n答案为 42") is None
    assert _extract_answer_from_output("the result is 42") is None
    assert _extract_answer_from_output("") is None
    assert _extract_answer_from_output(None) is None


def test_executor_takes_last_marker_and_keeps_multiline():
    # 取最后一个标记到末尾的全部内容（多行 SymPy 输出必须完整保留）。
    out = "最终答案: 1\n最终答案: 42\n[[1, 2],\n [3, 4]]"
    got = _extract_answer_from_output(out)
    assert got.startswith("42")
    assert "[3, 4]]" in got


@pytest.mark.parametrize("text,expected", [
    ("最终答案: 42", "42"),
    ("最终答案 ： 42", "42"),        # ← 修复目标
    ("答案是：B", "B"),
    ("答案为 42", "42"),
    ("结果为 42", "42"),
])
def test_extract_final_answer_covers_label_variants(text, expected):
    assert extract_final_answer(text) == expected
