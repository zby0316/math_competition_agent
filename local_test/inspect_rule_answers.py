# -*- coding: utf-8 -*-
"""检查错题本中 34 道规则可判定题的答案形态（数值/表达式/文本）。"""
import json
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

main_map = {}
with open("local_test/data/omni_math/test.jsonl", encoding="utf-8") as f:
    for i, line in enumerate(f):
        d = json.loads(line)
        main_map[d["problem"].strip()] = ("omni:%d" % i, d)

rule_by_problem = {}
with open("local_test/data/omni_math/omni_math_rule.jsonl", encoding="utf-8") as f:
    for line in f:
        if not line.strip():
            continue
        d = json.loads(line)
        rule_by_problem[d["problem"].strip()] = d

st = json.load(open("local_test/data/schedule/set1_state.json", encoding="utf-8"))
wrong_keys = [w["key"] for w in st.get("wrong", [])]

key_to_problem = {v[0]: k for k, v in main_map.items()}

NUM = re.compile(r"^-?\d[\d,\.]*(/[\d]+)?$|^\d+(\.\d+)?\s*[%％]$")
LATEX = re.compile(r"\\frac|\\sqrt|\\pi|\\pm|\\cdot|\\times|\\le|\\ge|\\in\b")

def shape(ans):
    a = ans.strip()
    if not a:
        return "empty"
    if NUM.match(a):
        return "numeric"
    if LATEX.search(a):
        return "latex-expr"
    if re.match(r"^[A-Za-z]$", a) or a in ("True", "False", "true", "false"):
        return "letter/bool"
    if len(a) <= 30 and not any(c.isalpha() for c in a):
        return "short-symbol"
    return "text"

print("=" * 100)
print("错题本中 34 道规则可判定题的答案形态")
print("=" * 100)
cnt = {"numeric": 0, "latex-expr": 0, "letter/bool": 0, "short-symbol": 0, "text": 0, "empty": 0}
for k in sorted(wrong_keys):
    problem = key_to_problem.get(k)
    if problem not in rule_by_problem:
        continue
    d = rule_by_problem[problem]
    ans = d.get("answer", "")
    s = shape(ans)
    cnt[s] += 1
    print("%s | %-12s | %-12s | ans=%.60s" % (k, d.get("domain", "?"), s, ans.replace("\n", " ")))
print("-" * 100)
print("形态分布:", cnt)
