# -*- coding: utf-8 -*-
"""比对：规则可判定题 vs 我们 4 轮用过的题。"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# 1) 主集 problem -> key
main_map = {}
with open("local_test/data/omni_math/test.jsonl", encoding="utf-8") as f:
    for i, line in enumerate(f):
        d = json.loads(line)
        main_map[d["problem"].strip()] = "omni:%d" % i

# 2) 规则子集 problem 集合
rule_problems = set()
with open("local_test/data/omni_math/omni_math_rule.jsonl", encoding="utf-8") as f:
    for line in f:
        if not line.strip():
            continue
        d = json.loads(line)
        rule_problems.add(d["problem"].strip())
print("规则子集 %d 题 | 主集匹配 %d 题" % (len(rule_problems),
      sum(1 for p in rule_problems if p in main_map)))

# 3) 我们 4 轮用过的题（key 集合）+ 当前错题本
used_keys = set()
for rnd in (1, 2, 3, 4):
    p = Path("local_test/data/schedule/set1_round_00%d.jsonl" % rnd)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                used_keys.add(json.loads(line)["key"])
st = json.load(open("local_test/data/schedule/set1_state.json", encoding="utf-8"))
wrong_keys = {w["key"] for w in st.get("wrong", [])}

# 4) 我们的题是否规则可判定（key -> problem 反查）
key_to_problem = {v: k for k, v in main_map.items()}
rule_used = [k for k in used_keys if key_to_problem.get(k) in rule_problems]
wrong_rule = [k for k in wrong_keys if key_to_problem.get(k) in rule_problems]
print("\n4 轮共用题 %d 道 | 其中规则可判定 %d 道 (%.0f%%)" % (
    len(used_keys), len(rule_used), len(rule_used) / len(used_keys) * 100))
print("当前错题本 %d 道 | 其中规则可判定 %d 道 (%.0f%%) ← 代码接管稳拿目标" % (
    len(wrong_keys), len(wrong_rule), len(wrong_rule) / len(wrong_keys) * 100))

# 5) 列出错题本中规则可判定题的 key
key_to_idx = {}
for rnd in (1, 2, 3, 4):
    p = Path("local_test/data/schedule/set1_round_00%d.jsonl" % rnd)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                key_to_idx[d["key"]] = (rnd, d["idx"])
print("\n错题本中规则可判定题：")
for k in sorted(wrong_rule):
    rnd, idx = key_to_idx.get(k, ("?", "?"))
    print("  round%d #%s" % (rnd, idx))
