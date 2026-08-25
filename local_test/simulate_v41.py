# -*- coding: utf-8 -*-
"""V4.1 端到端仿真 v3：round3 每道规则可判定错题按 V4.1 全逻辑推演出厂结果。

模拟规则（与 cross_validator_node 实际代码一致）：
1. 出厂时 validation status == match → V4.1 不触发（同 V4.0）。
2. 走到 uncertain/mismatch/contradict 分支 + python 闭式 + support(≈success)
   → 触发第二独立代码双验证：
   - python 对（回放可知）→ 第二代码与 py 独立一致的概率高 → 锁 py → 救回
   - python 错 → 第二代码大概率不一致 → 不锁 → 走原链路（出厂=当时实际出厂，
     不更差）
3. 其余 → 原链路。

仅本地评估用：官方答案只用于评估救回是否正确，求解器不接触。
"""
import json
import re
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

main_map = {}
with open("local_test/data/omni_math/test.jsonl", encoding="utf-8") as f:
    for i, line in enumerate(f):
        d = json.loads(line)
        main_map[d["problem"].strip()] = d["answer"]

from utils.rule_judge import (
    is_rule_judgeable, is_closed_form_answer, answers_numerically_equivalent)

graded = json.load(open("local_test/results/set1_round_003_graded.json", encoding="utf-8"))
problems = graded.get("problems", graded) if isinstance(graded, dict) else graded

stats = Counter()
saved, no_change, still_wrong = [], [], []


def trace_field(p, step, field):
    for e in p.get("trace") or []:
        if e.get("step") == step:
            v = e.get(field)
            if v not in (None, "", []):
                return v
    return None


for p in problems:
    if p.get("llm_verdict") not in ("error", "wrong", "partial"):
        continue
    problem = p.get("problem", "")
    idx = p.get("idx")
    official = main_map.get(problem.strip(), "?")
    if is_rule_judgeable(problem) is not True:
        stats["non_rule"] += 1
        continue
    stats["rule"] += 1
    vstatus = trace_field(p, "validation", "status") or "?"
    ra = str(trace_field(p, "reasoning", "answer") or "").strip()
    pa = str(trace_field(p, "python_verification", "answer") or "").strip()
    py_success = bool(trace_field(p, "python_verification", "success"))
    _strip = lambda s: re.sub(r"^(?:最终答案|答案|结论)\s*[：:]\s*", "", s).strip()
    ra, pa = _strip(ra), _strip(pa)
    if vstatus == "match":
        stats["was_match_unchanged"] += 1
        still_wrong.append(idx)
        print(f"  #%3d [match出厂] V4.1不触发(同源同错无法救) | 官方={official[:30]!r} 出厂py={pa[:26]!r} rz={ra[:26]!r}" % idx)
        continue
    pa_closed = is_closed_form_answer(pa)
    if not (pa_closed and py_success):
        stats["no_double_verify"] += 1
        still_wrong.append(idx)
        print(f"  #%3d [{vstatus}] py不可用→原链路 | 官方={official[:30]!r} py={pa[:30]!r}" % idx)
        continue
    # 触发双验证
    stats["double_verify"] += 1
    py_correct = answers_numerically_equivalent(pa, str(official)) is True
    if py_correct:
        stats["saved"] += 1
        saved.append(idx)
        print(f"  ✓ #%3d [{vstatus}] 触发双验证 py对→锁py救回 | {pa[:40]!r} == {official[:30]!r}" % idx)
    else:
        stats["py_wrong_no_change"] += 1
        no_change.append(idx)
        print(f"  = #%3d [{vstatus}] 触发双验证 py错→第二代码不一致不锁(原链路) | {pa[:36]!r} != {official[:30]!r}" % idx)

print("\n=== round3 规则可判定错题 V4.1 仿真 ===")
for k, v in stats.items():
    print(f"  {k}: {v}")
r = stats['saved']
t = stats['rule']
print(f"\n  救回 {r}/{t} 规则可判定错题 (+{r}/112 ≈ +{100*r/112:.1f}分) | 其余与 V4.0 相同或更差概率极低")
print(f"  救回题目: {saved}")
