# -*- coding: utf-8 -*-
"""V4.3 结论先行：章节顺序兼容性自测（独立脚本，避免 shell 转义干扰）。"""
import sys
sys.path.insert(0, ".")
from nodes.reasoning_agent_node import _parse_reasoning_output

# --- 真实模型输出形态（round3 trace 实证） ---

# 1. 新顺序 + 截断于步骤中途（最关键的防护场景）
resp1 = """## 最终答案
最终答案：$\\frac{19}{28}$

## 问题分析
这是条件概率题，用贝叶斯公式。

## 详细解题步骤
步骤1：定义事件
使用公式/定理：全概率公式
计算过程：P(A)=...
步骤2：应用贝叶斯（此处被输出截断"""
r1 = _parse_reasoning_output(resp1)
print("1) 新顺序+截断:", repr(r1["answer"]))
assert r1["answer"] and "19" in r1["answer"], "答案丢失!"
assert len(r1["steps"]) == 2

# 2. 新顺序 + 数值答案
resp2 = """## 最终答案
最终答案：12679605

## 问题分析
计数题。
## 详细解题步骤
步骤1：对称性
使用公式/定理：对称分解
计算过程：略"""
r2 = _parse_reasoning_output(resp2)
print("2) 新顺序+数值:", repr(r2["answer"]))
assert "12679605" in r2["answer"]

# 3. 新顺序 + LaTeX 根式答案
resp3 = """## 最终答案
最终答案：$-9+3\\sqrt{13}$

## 问题分析
代数题。"""
r3 = _parse_reasoning_output(resp3)
print("3) 新顺序+根式:", repr(r3["answer"]))
assert "sqrt{13}" in r3["answer"] or "sqrt(13)" in r3["answer"]

# 4. 旧顺序回归（答案在末尾）
resp4 = """## 问题分析
分析内容。

## 详细解题步骤
步骤1：推导
使用公式/定理：x
计算过程：y

## 最终答案
最终答案：42

## 关键验证点
- 代回验证"""
r4 = _parse_reasoning_output(resp4)
print("4) 旧顺序回归:", repr(r4["answer"]))
assert "42" in r4["answer"]
assert len(r4["steps"]) == 1
assert "代回验证" not in str(r4["steps"]), "验证点被吞进步骤"

# 5. 步骤章节后无其它章节（截断在步骤中间）：步骤不应吞掉无关内容
resp5 = """## 最终答案
最终答案：5

## 详细解题步骤
步骤1：设未知数
使用公式/定理：代数
计算过程：x+1=6 故 x=5"""
r5 = _parse_reasoning_output(resp5)
print("5) 步骤截断:", repr(r5["answer"]), "| steps:", len(r5["steps"]))
assert "5" in r5["answer"]

print("\n=== V4.3 章节顺序兼容性全部通过 ===")
