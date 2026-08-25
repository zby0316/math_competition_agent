# -*- coding: utf-8 -*-
"""V4.3 结论先行：章节顺序兼容性测试。

背景：REASONING_PROMPT 的 '## 最终答案' 章节移至全文第一个章节（截断保险）。
解析器按标题定位、与顺序无关，但步骤章节的 lookahead 曾依赖旧顺序
（(?=## 最终答案|$) 会把新顺序下的后续章节吞进步骤），已改为通用 ## 停靠。
"""
import sys

import pytest

sys.path.insert(0, ".")  # noqa: E402

from nodes.reasoning_agent_node import _parse_reasoning_output  # noqa: E402


def test_answer_first_truncated_keeps_answer():
    """核心防护：新顺序下输出被截断于步骤中途，答案仍完整保留。"""
    resp = """## 最终答案
最终答案：$\\frac{19}{28}$

## 问题分析
这是条件概率题，用贝叶斯公式。

## 详细解题步骤
步骤1：定义事件
使用公式/定理：全概率公式
计算过程：P(A)=...
步骤2：应用贝叶斯（此处被输出截断"""
    r = _parse_reasoning_output(resp)
    assert r["answer"] and "19" in r["answer"]
    assert len(r["steps"]) == 2


def test_answer_first_numeric():
    resp = """## 最终答案
最终答案：12679605

## 问题分析
计数题。
## 详细解题步骤
步骤1：对称性
使用公式/定理：对称分解
计算过程：略"""
    r = _parse_reasoning_output(resp)
    assert "12679605" in r["answer"]


def test_answer_first_sqrt():
    resp = """## 最终答案
最终答案：$-9+3\\sqrt{13}$

## 问题分析
代数题。"""
    r = _parse_reasoning_output(resp)
    assert "sqrt" in r["answer"]


def test_old_order_still_works():
    """回归：旧顺序（答案在末尾）解析不受影响，验证点不被吞进步骤。"""
    resp = """## 问题分析
分析内容。

## 详细解题步骤
步骤1：推导
使用公式/定理：x
计算过程：y

## 最终答案
最终答案：42

## 关键验证点
- 代回验证"""
    r = _parse_reasoning_output(resp)
    assert "42" in r["answer"]
    assert len(r["steps"]) == 1
    assert "代回验证" not in str(r["steps"])


def test_steps_truncated_without_other_sections():
    resp = """## 最终答案
最终答案：5

## 详细解题步骤
步骤1：设未知数
使用公式/定理：代数
计算过程：x+1=6 故 x=5"""
    r = _parse_reasoning_output(resp)
    assert "5" in r["answer"]
    assert len(r["steps"]) == 1


def test_answer_first_with_validation_points():
    """新顺序完整输出：四个章节各自正确解析。"""
    resp = """## 最终答案
最终答案：$\\frac{19}{28}$

## 问题分析
条件概率题。

## 详细解题步骤
步骤1：定义事件
使用公式/定理：全概率公式
计算过程：P(A)

## 关键验证点
- 代回验证：P(真|阳) 用贝叶斯重算一致"""
    r = _parse_reasoning_output(resp)
    assert "19" in r["answer"]
    assert len(r["steps"]) == 1
    assert len(r["validation_points"]) == 1
    assert "贝叶斯" in r["validation_points"][0]
