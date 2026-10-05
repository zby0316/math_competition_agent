"""回归：提示词模板回声不得作为答案出厂。

背景
----
reasoning 提示词里带着一份格式模板，用方括号标出每处待填位置：

    ## 结论速览
    \\boxed{[最终结论的一句话压缩版]}

    ## 问题分析
    [分析题目类型、关键信息、适用的定理/公式；…]
    步骤1：[描述]
    使用公式/定理：[具体公式]
    计算过程：[详细计算]

模型会把某一行**原样抄成答案**。实测：官方风格集 idx 102（参考答案是四元组
$(x,y,w,z)$）的 ``final_response`` 就是 ``[最终结论的一句话压缩版]``，
reasoning / python / validated_answer 三处全是它——该题必然判零分。

旧 ``_TEMPLATE_TOKEN_RE`` 要求关键词**紧跟左括号**（``[结论]`` 能匹配、
``[最终结论的一句话压缩版]`` 不能），且词表缺 ``计算/分析/题目`` 等，于是
模板里大半的占位符都判不出来。

修复分两层：
* **提示词**（根因）：模板前加明确说明——方括号内是"这里该写什么"的说明，
  必须整段替换，严禁照抄；
* **守卫**（安全网）：关键词允许出现在方括号内任意位置，词表补全。
真答案如 ``[0,1]``、``(1/3, 1]``、``[(0,-2),(2,0)]`` 不含模板词，不受影响。
"""

import pytest

from utils.cot_trim import is_placeholder_answer
from utils.response_builder import format_answer_for_output as fmt


# ---------- 模板里每一个占位符都必须被识别 ----------

@pytest.mark.parametrize("text", [
    "[最终结论的一句话压缩版]",                     # ← idx 102 实际出厂的形态
    "[分析题目类型、关键信息、适用的定理/公式；若题面有口径歧义，注明所采用口径]",
    "[描述]", "[具体公式]", "[详细计算]",
    "[结论]", "[答案]", "[填写]", "[此处]", "[你的答案]",
    "[验证点1：如何验证这个答案是正确的]",
    "【最终结论的一句话压缩版】",
])
def test_template_placeholders_are_detected(text):
    assert is_placeholder_answer(text) is True


@pytest.mark.parametrize("text", [
    "[最终结论的一句话压缩版]", "[详细计算]", "[描述]",
])
def test_template_placeholders_are_cleared_in_output(text):
    assert fmt(text, "computation") == ""


# ---------- 安全底线：真答案不受影响 ----------

@pytest.mark.parametrize("text", [
    "[0,1]", "[0, 1)", "(1/3, 1]", "(-\\infty, 0]",
    "[(0, -2), (2, 0)]", "[[1, 2], [3, 4]]", "[a, b]",
    "\\{1,2,3\\}", "x \\in [0,1]",
    "72", "n!", "\\lambda \\ge 1", "(2,0),(0,-2)",
    "\\begin{pmatrix} 1 & 2 \\\\ 3 & 4 \\end{pmatrix}",
])
def test_real_answers_are_not_placeholders(text):
    assert is_placeholder_answer(text) is False


def test_real_answers_survive_output_formatting():
    assert fmt("[0,1]", "computation") == "[0, 1]"
    assert fmt("[(0, -2), (2, 0)]", "computation") == "[(0, -2), (2, 0)]"
    assert fmt("n!", "computation") == "factorial(n)"


# ---------- 提示词根因修复 ----------

def test_prompt_warns_against_copying_the_template():
    from utils.prompt_bank import REASONING_PROMPT
    rendered = REASONING_PROMPT.format(
        skill_document="（技能文档）", problem="1+1=?", category="数学分析",
        question_mode="computation", validation_status="无")
    # 警告必须存在，且必须出现在模板本体之前。
    warn_at = rendered.find("严禁原样照抄")
    template_at = rendered.find("\\boxed{[最终结论")
    assert warn_at != -1, "提示词缺少禁止照抄模板的说明"
    assert template_at != -1, "提示词模板缺失"
    assert warn_at < template_at, "警告必须排在模板之前才起作用"
