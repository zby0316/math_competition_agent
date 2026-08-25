# -*- coding: utf-8 -*-
"""V4.1 规则接管离线回放 v2：从 trace 提取 reasoning/python 候选，模拟接管。

注意：本回放仅用于**本地评估**接管收益——用 rule 文件的官方答案判断
接管是否选对。求解器自身逻辑（cross_validator/python_agent）不读取
官方答案，回放脚本与求解管线完全分离。
"""
import json
import re
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

# 官方答案表（仅评估用）
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
saved, missed, wrong_takeover = [], [], []


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
        stats["error_non_rule"] += 1
        continue
    stats["error_rule"] += 1
    ra = str(trace_field(p, "reasoning", "answer") or "").strip()
    pa = str(trace_field(p, "python_verification", "answer") or "").strip()
    py_success = bool(trace_field(p, "python_verification", "success"))
    _strip = lambda s: re.sub(r"^(?:最终答案|答案|结论)\s*[：:]\s*", "", s).strip()
    ra, pa = _strip(ra), _strip(pa)
    ra_closed = is_closed_form_answer(ra)
    pa_closed = is_closed_form_answer(pa)
    # V4.1 新接管逻辑：推理非闭式 + python 闭式 + 执行成功 → 触发第二独立
    # 代码双验证（回放只能评估触发面；双验证锁对需要第二份代码独立一致——
    # python 对则大概率救回，python 错则大概率不一致不锁）。
    if ra_closed:
        stats["rz_closed_no_takeover"] += 1
        print(f"  #%3d 推理闭式→交回原链路 | 官方={official[:34]!r} | py={pa[:36]!r} | rz={ra[:36]!r}" % idx)
    elif pa_closed and py_success:
        stats["double_verify_triggered"] += 1
        ok = answers_numerically_equivalent(pa, str(official)) is True
        if ok:
            stats["py_correct"] += 1
            saved.append(idx)
            print(f"  ✓ #%3d 触发双验证且python对→大概率救回 | {pa[:40]!r} == {official[:34]!r}" % idx)
        else:
            stats["py_wrong"] += 1
            wrong_takeover.append(idx)
            print(f"  ⚠ #%3d 触发双验证但python错→第二代码不一致则不锁 | {pa[:40]!r} != {official[:34]!r}" % idx)
    else:
        stats["rule_no_takeover"] += 1
        missed.append(idx)
        print(f"  #%3d 无法接管 | 官方={official[:34]!r} | py={pa[:36]!r} | rz={ra[:36]!r}" % idx)

print("\n=== round3 错题规则接管回放 v2 ===")
for k, v in stats.items():
    print(f"  {k}: {v}")
print(f"  救回 {stats['saved']} / 规则可判定错题 {stats['error_rule']}"
      f" → 救回率 {100*stats['saved']/max(1, stats['error_rule']):.0f}%")
print(f"  救回题目: {saved}")
