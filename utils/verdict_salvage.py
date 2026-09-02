"""从没有写出 Markdown 小标题的响应里，捞回一句明确的结论。

部分推理模型把 ``reasoning_content`` 计入 ``max_tokens``，遇到难题会在思考上
耗尽全部预算。实测故障比单纯截断更糟：模型在正文里一路推理*到结论*，却在写出
``## 最终答案`` 之前就没了空间，导致只认标题的解析器把这次尝试判为"什么也没
产出"。在竞赛级博弈论题上实测三次：推理正文分别有 41,424 / 42,810 / 43,055
字符，解析器每次都提取不到任何小节；其中两次在 1072s、1165s 后以
``解题过程中出现错误`` 收场——模型其实做完了工作，只是被流水线丢弃。

这里刻意作为*最后手段*使用，仅在按标题解析拿不到答案时启用：
  * 要求有明确的结论标记（因此/所以/综上/故/答案是），普通探索性散文
    （"我们先试 a=1"）不会被误认成结论；
  * 只取最后一处这样的句子，因为长推导会在落定前反复回访中间论断；
  * 每个候选都要和解析出的答案走同一道占位/碎片门，捞回值的质量
    不可能低于正经小节产出的答案。

捞回的答案会在追踪里打上 ``answer_source="salvaged_prose"`` 标记，
便于与格式正确的答案区分开。
"""

from __future__ import annotations

import re

#: 宣布结论而非探索过程的句子。`因此`/`所以`/`综上` 是可靠的中文结论标记；
#: `答案` 覆盖显式自标注。要求命中其一，才能把探索性散文挡在门外。
_RESULT_OPENER_RE = re.compile(
    r"(?:因此|所以|综上|故此|故\s|由此可(?:知|得)|答案(?:是|为)|结论(?:是|为|[：:])"
    r"|可以证明|我们(?:得到|证明了)|最终(?:答案|结论))")

#: 结论必须携带信息：等式、数值、LaTeX 量，或明确的定性判断。
#: 纯连接性散文（"因此我们继续"）会被丢弃。
_PROSE_PAYLOAD_RE = re.compile(
    r"[=<>]|\d|\\frac|\\sqrt|\\lambda|\\pi|π|必胜|收敛|发散|成立|不成立|存在|唯一"
    r"|拒绝|接受|最大|最小|充分|必要")

#: 句子边界。换行与句号都算，但小数点不能把 `0.5` 拆开，
#: 所以对英文句点用前后瞻保护。
_CLAUSE_BOUNDARY_RE = re.compile(r"(?:[。；;!?！？\n]|(?<!\d)\.(?!\d))+")

#: 长度下限保证"是一句论断"，上限保证"是答案而不是一段话"。
_MIN_CHARS = 6
_MAX_CHARS = 400

#: 只看末尾多少句。推理链的结论落在结尾；扫全篇 42k 字符的正文会引入
#: 推导中途的论断。
_TAIL_SENTENCES = 40


def _sentences(text: str) -> list[str]:
    return [s.strip().strip("*# 　") for s in _CLAUSE_BOUNDARY_RE.split(text or "")
            if s and s.strip()]


def recover_verdict_sentence(response: str) -> str:
    """返回 `response` 中最像结论的一句，没有则返回 ''。

    仅在按标题解析已经失败时使用——见模块 docstring。
    """
    from utils.answer_sanitize import is_junk, is_derivation_fragment
    from utils.answer_pull import looks_incomplete_answer, looks_like_latex_fragment
    from utils.cot_trim import is_placeholder_answer

    text = response or ""
    if not text.strip():
        return ""

    candidates = _sentences(text)[-_TAIL_SENTENCES:]
    # 从后往前扫：长推导会在收尾前反复复述中间结果。
    for sentence in reversed(candidates):
        if not (_MIN_CHARS <= len(sentence) <= _MAX_CHARS):
            continue
        if not _RESULT_OPENER_RE.search(sentence):
            continue
        if not _PROSE_PAYLOAD_RE.search(sentence):
            continue
        if is_placeholder_answer(sentence) or looks_like_latex_fragment(sentence) \
                or looks_incomplete_answer(sentence):
            continue
        # 洁净度门：带结论词但实为自我怀疑/元叙述的句子
        # （"所以哪里出错了？"）不得作为答案输出。
        if is_junk(sentence):
            continue
        # 对账门：条件对/裸符号和/true-false 试错记录这类推导现场碎片，
        # 带结论词也不得出厂。
        if is_derivation_fragment(sentence):
            continue
        return sentence
    return ""
