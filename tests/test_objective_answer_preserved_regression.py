"""回归：客观题"归一化失败"不得清空仍然可能有效的答案。

背景
----
``_parse_reasoning_output`` 末尾对客观题做归一化，归一化失败时**无条件清空**：

    normalized = normalize_objective_answer(r["answer"], question_mode)
    if objective_answer_is_usable(normalized, question_mode):
        r["answer"] = normalized
    else:
        r["answer"] = ""          # ← 把答案整个丢掉

实测（修复前）以下四种情形答案被清空，该题必然判 0：

* ``choice``     + ``直方图`` / ``以上都有可能`` —— 模型直接答选项原文而没写字母
* ``true_false`` + ``True`` / ``是``            —— 归一化只认中文判断词

修复保留原文（仅当原文本体是垃圾时才丢弃）；``fill`` 保持原样清空，因为它的
严格过滤是刻意设计，且清空后会走压缩重试去拿真实空位内容。
"""

from nodes.reason_step import _parse_reasoning_output


def _answer(mode: str, text: str) -> str:
    return _parse_reasoning_output(f"## 最终答案\n{text}", mode)["answer"]


def test_choice_option_text_is_preserved():
    assert _answer("choice", "直方图") == "直方图"
    assert _answer("choice", "以上都有可能") == "以上都有可能"


def test_choice_letters_are_still_normalised():
    assert _answer("choice", "A") == "A"
    assert _answer("choice", "ABF") == "A、B、F"


def test_true_false_english_and_short_forms_are_normalised():
    """判断题的全部常见答法都要归一化成标准形，而不是保留原文。

    本测试原先是 ``assert _answer("true_false", "True") == "True"``——那编码的是
    "归一化失败则保留原文"的旧行为：``True``/``是`` 因为不在词表里而原样出厂。
    词表补全后它们被归一化，交付形态与判分器的参考形（``正确``/``错误``）精确一致，
    比原样输出更易被判对。**保留原文的底线没有丢**：本体是垃圾时仍然丢弃
    （见 test_choice_junk_is_still_dropped）。
    """
    assert _answer("true_false", "True") == "正确"
    assert _answer("true_false", "是") == "正确"
    assert _answer("true_false", "成立") == "正确"
    assert _answer("true_false", "√") == "正确"
    assert _answer("true_false", "False") == "错误"
    assert _answer("true_false", "否") == "错误"
    assert _answer("true_false", "不成立") == "错误"
    assert _answer("true_false", "×") == "错误"
    # 原有的中文判断词行为不变
    assert _answer("true_false", "对") == "正确"
    assert _answer("true_false", "错") == "错误"


def test_choice_junk_is_still_dropped():
    assert _answer("choice", "无法确定") == ""
    assert _answer("choice", "N/A") == ""


def test_fill_behaviour_is_unchanged():
    # 术语填空照常保留。
    assert _answer("fill", "异方差性") == "异方差性"
    # 不确定类回答照常清空（清空后才能触发压缩重试拿真实空位内容）。
    assert _answer("fill", "不确定") == ""
    assert _answer("fill", "不一定") == ""
