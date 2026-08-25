# -*- coding: utf-8 -*-
"""V4.4：emergency 路径纳入异构第二路（diverse）产出。

set1 round4 实证 #3/#46：主推理空产出 + Python 失败后，diverse_reasoning
（异构第二路）给出了完整分析+步骤+答案，但 coordinator 的 emergency 组装
完全不看 reasoning_result_alt，最后只输出一行 emergency 裸猜答案——
"答案+推导"形态被白白丢弃。本测试锁定修复行为。
"""
import sys

import pytest

sys.path.insert(0, ".")  # noqa: E402

from nodes.coordinator_node import (  # noqa: E402
    _diverse_clues,
    _partial_response,
    _with_emergency_answer,
)


def _state_with_diverse():
    return {
        "reasoning_result": {"answer": "", "analysis": "", "steps": []},
        "reasoning_raw_response": "",
        "reasoning_result_alt": {
            "answer": "最终答案：3",
            "analysis": "本题是布尔格上的函数方程计数问题。关键观察：f 具有乘法形式。",
            "steps": [
                {"step_num": 1, "description": "由模格方程推导 f 的乘法形式"},
                {"step_num": 2, "description": "枚举小规模染色方案对照"},
            ],
        },
        "python_output": {"answer": "", "stdout": "小规模暴力枚举结果: {1: 4, 2: 10}"},
    }


def test_diverse_clues_extracts_answer_first():
    t, src = _diverse_clues(_state_with_diverse())
    assert src == "diverse_alt"
    lines = t.splitlines()
    assert lines[0] == "3"  # 答案行在首位（去掉了"最终答案："前缀）


def test_diverse_clues_empty_when_no_alt():
    t, src = _diverse_clues({"reasoning_result_alt": {}})
    assert t == "" and src == ""


def test_diverse_clues_no_prefix_duplication():
    state = _state_with_diverse()
    state["reasoning_result_alt"]["answer"] = "最终答案：42"
    t, _ = _diverse_clues(state)
    assert t.splitlines()[0] == "42"
    assert not t.splitlines()[0].startswith("最终答案：最终答案")


def test_partial_response_prefers_diverse():
    partial, src = _partial_response(_state_with_diverse())
    assert src == "diverse_alt"
    assert "3" in partial
    assert "乘法形式" in partial  # 分析与步骤也带上


def test_emergency_assembly_uses_diverse_as_body():
    partial, src = _partial_response(_state_with_diverse())
    final = _with_emergency_answer("$(n+2)2^n - 1$", partial, src)
    assert final.startswith("最终答案：3")  # diverse 答案为主体
    assert "解题过程参考" in final  # 推导附后
    assert "补充尝试" in final  # 裸猜直答降级为补充
    assert "n+2" in final


def test_emergency_assembly_legacy_guess_path():
    """回归：无 diverse 时保持原行为（guess 为主体）。"""
    final = _with_emergency_answer("42", "未能完成完整推导，以下为已获得的部分结果：\n7", "partial_findings")
    assert final.startswith("最终答案：42")
    assert "部分结论" in final


def test_emergency_assembly_generic_error():
    final = _with_emergency_answer("42", "最终答案：未能求解", "generic_error")
    assert final == "最终答案：42"
