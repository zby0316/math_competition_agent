"""回归：客观题的"裸载荷"兜底不得把章节标题或正文步骤当成答案。

背景
----
``_parse_reasoning_output`` 里有一段为「模型漏掉 ``答案：`` 标签、把载荷单独放一行」
准备的兜底（``answer_source="bare_objective_line"``）。它原本对**任何**客观题回复
逐行倒序扫描，而 ``normalize_objective_answer(..., "fill")`` 对任意文本原样返回，
于是结构行与正文行都能通过填空可用性检查。

实测（修复前）：

    '## 最终答案\\n无解'                        -> '## 最终答案'   章节标题成了答案
    '## 问题分析…## 详细解题步骤…## 最终答案\\n无法确定'
                                               -> '步骤1：代入'   正文步骤成了答案

两者都会产出 ``最终答案：## 最终答案`` 这类必然判 0 的交付文本。

修复：兜底只对**完全没有章节结构（不含 ``##``）的裸回复**生效；带章节的回复其载荷
由 ``_distill_answer`` 负责。循环内再跳过 ``#`` 开头的行作为第二道防线。
"""

from nodes.reason_step import _parse_reasoning_output


def _answer(response: str, mode: str):
    result = _parse_reasoning_output(response, mode)
    return result.get("answer", ""), result.get("answer_source")


def test_section_header_is_never_the_answer():
    for text in ("无法确定", "无解", "无法求解", "不能确定"):
        answer, _ = _answer(f"## 最终答案\n{text}", "fill")
        assert "最终答案" not in answer, (text, answer)


def test_body_step_is_not_harvested_from_structured_reply():
    response = ("## 问题分析\n由对称性可知。\n"
                "## 详细解题步骤\n步骤1：代入。\n"
                "## 最终答案\n无法确定")
    answer, _ = _answer(response, "fill")
    assert "步骤" not in answer, answer
    assert answer == ""


def test_bare_payload_fallback_still_works():
    assert _answer("B", "choice")[0] == "B"
    assert _answer("A", "choice")[0] == "A"
    assert _answer("正确", "true_false")[0] == "正确"
    assert _answer("异方差性", "fill")[0] == "异方差性"
    assert _answer("答案：C", "choice")[0] == "C"


def test_structured_reply_with_valid_answer_still_works():
    assert _answer("## 最终答案\n异方差性", "fill")[0] == "异方差性"
    assert _answer("## 最终答案\n直方图", "choice")[0] == "直方图"
    assert _answer("## 最终答案\nA", "choice")[0] == "A"
