"""回归：独立审查第二轮（针对第六、七轮）发现的四处回归。

背景
----
第六、七轮的修复方向成立，但收紧判定面时各自引入新漏判，且都已推送到 main。
三方对照（第五轮基线 `0f16e2ab` / 已推送 `20450953` / 本轮修复后）实测：

| 检查项 | 第五轮 | 已推送 HEAD | 修复后 |
|---|---|---|---|
| 真选择题（求解动词 + 点号选项） | choice | **fill** | choice |
| 冒号 / 右括号分隔的选项 | choice | **computation** | choice |
| 连接词形态的零计数 | support | **contradict** | support |
| 同行零计数 + 真实反例（中文） | contradict | **support** | contradict |
| 非标准小标题的裸载荷 | 有答案 | **空** | 有答案 |

四处根因：
1. 第六轮把"求解动词否决"从括号族扩展到了所有族，真选择题被判成 fill；
2. 判定用的分隔符集合比上游 `_OPTION_RE` 窄（少了 `: ： )`），官方常见写法被否；
3. 零计数正则收紧过度，漏掉 `found/observed/的个数` 连接词，零失败行重新被判反例；
4. 第七轮的 `"##" not in response` 门槛比解析器真正认识的章节集宽，
   `## 答案` / `## 结论` 这类裸载荷被误挡。

本文件用**强断言**锁死修复后的行为（第六轮那份测试用的是 `!= "choice"`，
恰好放过了"choice 变 fill"这类漏判）。
"""

from nodes.reason_step import _parse_reasoning_output
from utils.question_profile import classify_question_mode
from utils.verdict_evidence import parse_verification_evidence


# ---------- 1. 带求解动词的真选择题必须是 choice（不能退化成 fill/computation）----------

def test_mcq_with_solving_verb_is_choice_not_fill():
    for text in (
        "若 a+b=10，则 ab 的最大值是（ ）A. 25 B. 20 C. 50 D. 100",
        "求 P(X>0) 的值（ ）A. 0.5 B. 0.3 C. 0.2 D. 0.1",
    ):
        assert classify_question_mode(text) == "choice", text


def test_options_with_colon_or_right_paren_separators_are_choice():
    for text in (
        "A：均值 B：方差 C：标准差",
        "Which is correct? A: x B: y C: z",
        "A) x B) y",
    ):
        assert classify_question_mode(text) == "choice", text


# ---------- 2. 英文变量仍不得判选择题（第六轮的修复目标）----------

def test_english_variable_letters_are_not_options():
    for text in (
        "Simplify F. 2 and G. 3 expressions.",
        "H. 2 + G. 3 = 5",
        "Let (H) and (G) be the given pairs of points.",
    ):
        assert classify_question_mode(text) != "choice", text


def test_matrix_label_question_with_three_labels_is_not_choice():
    assert classify_question_mode(
        "设 (A)(B)(C) 是三个矩阵，求 det(ABC) 的最大值。") != "choice"


# ---------- 3. 零计数：连接词形态要拦、混合行要保留真反例 ----------

def _evidence(line: str) -> str:
    return parse_verification_evidence(
        {"stdout": f"验证状态: PASS\n{line}\n", "success": True, "answer": "99"},
        candidate_answer="42",
    )["evidence_status"]


def test_connector_form_zero_counts_do_not_contradict():
    for line in ("contradictions found: 0", "failures observed: 0",
                 "反例的个数: 0", "fail_count: 0", "0 mismatches", "反例: 0"):
        assert _evidence(line) != "contradict", line


def test_zero_count_on_same_line_keeps_real_counterexample():
    assert _evidence("0 个反例；x=2 处不一致") == "contradict"
    assert _evidence("0 errors; 1 counterexample at x=2") == "contradict"


def test_real_counterexample_with_zero_coordinate_is_still_kept():
    assert _evidence("1 contradiction found at t=0") == "contradict"


# ---------- 4. 裸载荷兜底：非标准小标题要救回，标准章节不得被捞 ----------

def test_non_standard_heading_bare_payload_is_recovered():
    assert _parse_reasoning_output("## 答案\nB", "choice")["answer"] == "B"
    assert _parse_reasoning_output("## 结论\nA、C", "choice")["answer"] == "A、C"


def test_standard_section_headers_are_never_the_answer():
    for response in (
        "## 最终答案\n无解",
        "## 问题分析\n由对称性可知。\n## 详细解题步骤\n步骤1：代入。\n## 最终答案\n无法确定",
    ):
        answer = _parse_reasoning_output(response, "fill")["answer"]
        assert "最终答案" not in answer, answer
        assert "步骤" not in answer, answer


def test_plain_bare_payload_fallback_still_works():
    assert _parse_reasoning_output("B", "choice")["answer"] == "B"
    assert _parse_reasoning_output("正确", "true_false")["answer"] == "正确"
    assert _parse_reasoning_output("异方差性", "fill")["answer"] == "异方差性"
