"""回归：独立代码审查发现的三个遗留问题。

来源：对三轮改动的独立 code review（2026-10-04）。三条均经本机复现后修复。

1. **[P2] 英文题面里的"变量字母 + 句点"被当成选项标签**，计算题被误判成选择题。
   字母表从 A–E 放宽到 A–H 后，`E/F/G/H` 这些英文数学文本里最常用的变量名进入了
   命中集。实测：4 个英文计算题在放宽前全判 ``computation``，放宽后全变 ``choice``；
   而单选题模式会强制模型只回一个选项字母，计算题的答案直接作废。
   修法：真选项列表**从 A 开始**（``_looks_like_option_list``），且二次裁决不再无条件
   兜底返回 True。

2. **[P2] 零值防护吞掉真实反例**：旧写法允许关键词与 0 之间夹任意文本，
   ``1 contradiction found at t=0`` 这种"反例 + 坐标=0"的混合行被整行跳过，
   实测返回 ``support`` 且 ``contradictions`` 为空——真实反例证据被静默丢弃。
   修法：只认真正的计数形态（关键词紧邻分隔符），且不收单数 ``counterexample``
   （``counterexample: 0`` 更像"反例取值是 0"）。

3. **[P3] 逐空标签只认阿拉伯数字**，``第一空/第二空`` 中文序数仍会只剩最后一空——
   正是第 6 项修复要消灭的故障形态；策略 2.7 也救不了（带 CJK 不算裸值行）。
"""

from nodes.reason_step import _parse_reasoning_output
from utils.question_profile import classify_question_mode
from utils.verdict_evidence import parse_verification_evidence


# ---------- 1. 英文计算题不得被判选择题 ----------

def test_english_computation_is_not_misread_as_choice():
    for text in (
        "Simplify F. 2 and G. 3 expressions.",
        "Given matrix H. 2 and E. 3 compute the determinant.",
        "H. 2 + G. 3 = 5",
        "Let (H) and (G) be the given pairs of points.",
    ):
        assert classify_question_mode(text) != "choice", text


def test_real_choice_questions_are_still_detected():
    for text in (
        "下列各式中正确的是 A. 1 B. 2 C. 3 D. 4",
        "Which is correct? A. x B. y",
        "单选题：A.均值 B.方差 C.标准差",
        "求下列各式的值（A.均值B.方差C.标准差）",
    ):
        assert classify_question_mode(text) == "choice", text


def test_matrix_label_question_is_not_choice():
    # (A)(B)(C) 是矩阵名而不是选项，且含求解动词。
    assert classify_question_mode(
        "设 (A)(B)(C) 是三个矩阵，求 det(ABC) 的最大值。") != "choice"


# ---------- 2. 真实反例不得被零值防护吞掉 ----------

def _evidence(line: str) -> str:
    return parse_verification_evidence(
        {"stdout": f"验证状态: PASS\n{line}\n", "success": True, "answer": "99"},
        candidate_answer="42",
    )["evidence_status"]


def test_real_counterexample_with_zero_coordinate_is_kept():
    assert _evidence("1 contradiction found at t=0") == "contradict"
    assert _evidence("counterexample: 0 means the value at x=0 differs") == "contradict"


def test_genuine_zero_counts_are_still_skipped():
    for line in ("fail_count: 0", "0 mismatches", "反例: 0", "反例个数：0",
                 "counterexample_count: 0"):
        assert _evidence(line) != "contradict", line


# ---------- 3. 中文序数逐空标签 ----------

def _fill(text: str) -> str:
    return _parse_reasoning_output(f"## 最终答案\n{text}", "fill")["answer"]


def test_chinese_ordinal_blank_labels_keep_all_slots():
    out = _fill("第一空: 16\n第二空: 0\n第六空: 65536")
    for token in ("16", "0", "65536"):
        assert token in out, out


def test_mixed_arabic_and_chinese_blank_labels_keep_all_slots():
    out = _fill("空1: 16\n第二空：0\n空3: 2")
    for token in ("16", "0", "2"):
        assert token in out, out
