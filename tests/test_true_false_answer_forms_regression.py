"""回归：判断题的全部常见答法必须归一化（旧词表只认 8 种）。

背景
----
``normalize_objective_answer(..., "true_false")`` 的词表原先是

    r"不正确|错误|正确|不对|对|错|×|√"

于是下列**标准答法**全部落空、归一化返回空串，``objective_answer_is_usable``
随即判为"答案不可用"——**模型答对了却被当成没作答**。实测（修复前）：

    normalize_objective_answer('是',    'true_false') -> ''
    normalize_objective_answer('否',    'true_false') -> ''
    normalize_objective_answer('T',     'true_false') -> ''
    normalize_objective_answer('F',     'true_false') -> ''
    normalize_objective_answer('True',  'true_false') -> ''
    normalize_objective_answer('False', 'true_false') -> ''
    normalize_objective_answer('成立',   'true_false') -> ''
    normalize_objective_answer('不成立', 'true_false') -> ''
    normalize_objective_answer('yes',   'true_false') -> ''

另有一个独立缺陷：``√ × ✓ ✗`` 在 Unicode 里属于 ``\\W``，只含它们的答案被
``_PUNCT_ONLY_RE`` 判成"纯标点占位符"，``format_answer_for_output`` 返回空串：

    is_placeholder_answer('√') -> True    （正确答案被当占位符丢掉）

两者都属于"把对答案变成错答案"，确定性可修。
"""

import pytest

from nodes.reason_step import _parse_reasoning_output
from utils.cot_trim import is_placeholder_answer
from utils.question_profile import (normalize_objective_answer,
                                    objective_answer_is_usable)


def _answer(mode: str, text: str) -> str:
    return _parse_reasoning_output(f"## 最终答案\n{text}", mode)["answer"]


# ---------- 修复目标：判断题全部常见答法 ----------

@pytest.mark.parametrize("text", [
    "正确", "对", "√", "✓", "是", "成立", "真的", "T", "t", "True", "true", "yes", "Y",
])
def test_true_forms_normalise_to_correct(text):
    assert normalize_objective_answer(text, "true_false") == "正确"
    assert objective_answer_is_usable(text, "true_false") is True


@pytest.mark.parametrize("text", [
    "错误", "错", "×", "✗", "否", "不成立", "假的", "不正确", "不对",
    "F", "f", "False", "false", "no", "N",
])
def test_false_forms_normalise_to_wrong(text):
    assert normalize_objective_answer(text, "true_false") == "错误"
    assert objective_answer_is_usable(text, "true_false") is True


def test_negation_phrase_wins_over_positive_substring():
    # "不成立"含"成立"、"不对"含"对"——必须判为错误。
    assert normalize_objective_answer("不成立", "true_false") == "错误"
    assert normalize_objective_answer("不对", "true_false") == "错误"
    assert normalize_objective_answer("不正确", "true_false") == "错误"


def test_first_judgement_word_wins():
    # 后面跟解释句时，第一个判断词才是答案。
    assert normalize_objective_answer("错误，因为反例 x=2 使结论正确", "true_false") == "错误"


# ---------- 修复目标：√ × 不是占位符 ----------

@pytest.mark.parametrize("text", ["√", "×", "✓", "✗", "  √  ", "√×"])
def test_check_symbols_are_not_placeholders(text):
    assert is_placeholder_answer(text) is False


@pytest.mark.parametrize("text,expected", [
    ("√", "正确"), ("✓", "正确"), ("×", "错误"), ("✗", "错误"),
])
def test_check_symbols_survive_the_whole_chain(text, expected):
    assert _answer("true_false", text) == expected


# ---------- 不得回归：真正的占位符仍须拦下 ----------

@pytest.mark.parametrize("text", [
    "", "   ", "...", "。", "、", "[结论]", "none", "N/A", "同上",
    "无法确定", "[final conclusion]", "placeholder", "todo",
])
def test_real_placeholders_still_detected(text):
    assert is_placeholder_answer(text) is True


def test_choice_and_fill_behaviour_unchanged():
    assert normalize_objective_answer("ABF", "choice") == "A、B、F"
    assert _answer("choice", "A") == "A"
    assert _answer("choice", "无法确定") == ""
    assert _answer("fill", "异方差性") == "异方差性"
