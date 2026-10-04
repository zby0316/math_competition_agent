"""回归：不得从长散文/解答里扫描出选项字母。

背景
----
把选项字母范围扩到 A–H 之后，逐字母扫描会把英文解答里的孤立 f、g、h（以及原本
就命中的 a）当成选项。差分验证（同一批输入分别喂原始版与补丁版）实测：

    基准题 umath_text24#2 的英文解答  ->  旧 'A'，新 'F、A、H'
    proof_gap12 两道证明长文         ->  旧 'E' / 'C、A'，新 'E、F' / 'F、C、G、A'

旧行为同样是错的（从散文里抓出 'A'），新范围只是错得更多，因此不能靠回退字母范围
解决。修复改为：逐字母扫描只对 ≤ ``_CHOICE_MAX_SCAN_CHARS``(60) 的短文本生效——
真正的选项串都极短，长文本按解答处理，交给判分器看原文。
"""

from utils.question_profile import normalize_objective_answer


def test_long_english_solution_is_not_scanned_for_letters():
    text = "1. The average rate of change for $f$ near $a$ with $h=0.1$ is: $7.8$"
    assert len(text) > 60
    assert normalize_objective_answer(text, "choice") == ""


def test_long_chinese_solution_is_not_scanned_for_letters():
    text = ("(1) 若不同的 $x,y$ 不能被不交开邻域分开，取两点的可数开邻域基，"
            "并以有限交改为递减基 $U_n,V_n$。每个 $U_n\\cap V_n$ 非空。")
    assert len(text) > 60
    assert normalize_objective_answer(text, "choice") == ""


def test_short_option_strings_still_work():
    assert normalize_objective_answer("F", "choice") == "F"
    assert normalize_objective_answer("ABF", "choice") == "A、B、F"
    assert normalize_objective_answer("A、C、F", "choice") == "A、C、F"
    assert normalize_objective_answer("ABCDEF", "choice") == "A、B、C、D、E、F"


def test_short_labelled_answers_still_work():
    assert normalize_objective_answer("答案为 A", "choice") == "A"
    assert normalize_objective_answer("选项 F", "choice") == "F"
    assert normalize_objective_answer("1. A；2. C", "choice") == "A、C"
