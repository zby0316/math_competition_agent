# -*- coding: utf-8 -*-
"""112 题/轮 测试调度器（V3 验证体系）

规则（用户需求）：
1. 每轮固定 112 题，轮次之间题目不重样（全局 used 池防重复）；
2. 错题进"错题本"，下一轮优先重测，直到做对才移出；
3. 按版本分两套测试：set1（默认 main / V2.5.8）、set2（默认 v32 / V3.2），
   各自独立维护进度与错题本；两套之间抽题互斥（可配置）。

题库：Omni-MATH 4428 题（local_test/data/omni_math/test.jsonl），
按官方 18 科分布 + 难度带（easy 1-3.5:30% / medium 4-5.5:45% / hard 6-9.5:25%）抽样。

用法：
    python local_test/round_manager.py init   --set 1 --version main --seed 1001
    python local_test/round_manager.py next   --set 1            # 生成下一轮 112 题
    python local_test/round_manager.py submit --set 1 --round 3 --results <path.jsonl>
    python local_test/round_manager.py stats  --set 1

结果文件格式（每行 JSON）：{"key": "omni:123", "verdict": "correct"|"wrong"|"partial"|"error"}
"""
import argparse
import json
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_HERE = Path(__file__).resolve().parent
_DATA = _HERE / "data"
_OMNI = _DATA / "omni_math" / "test.jsonl"
_SCHED = _DATA / "schedule"
_SCHED.mkdir(parents=True, exist_ok=True)

ROUND_SIZE = 112

# 官方 18 科分布（FAQ 公开，112 题基准）
OFFICIAL_DIST = {
    "离散数学": 24, "数值分析": 13, "测度积分": 11, "微分几何": 9,
    "概率论": 8, "抽象代数": 8, "随机过程": 7, "复分析": 7,
    "常微分方程": 5, "统计推断": 4, "泛函分析": 4, "线性回归": 3,
    "偏微分方程": 3, "非基础及进阶": 2, "高等代数": 1, "运筹学": 1,
    "数学分析": 1, "拓扑学": 1,
}

# Omni-MATH domain → 官方科目映射（关键词，大小写不敏感；空列表 = 无映射）
DOMAIN_MAP = {
    "离散数学": ["combinatorics", "graph theory", "discrete mathematics", "counting", "permutation", "tiling"],
    "数值分析": ["numerical analysis", "approximation", "root finding", "interpolation", "quadrature"],
    "测度积分": ["measure", "lebesgue", "integral", "integration", "riemann"],
    "微分几何": ["differential geometry", "curvature", "manifold", "surface"],
    "概率论": ["probability", "expected value", "random variable", "dice"],
    "抽象代数": ["abstract algebra", "group theory", "field theory", "ring", "finite field", "galois"],
    "随机过程": ["stochastic", "markov", "random walk", "martingale"],
    "复分析": ["complex analysis", "complex number", "holomorphic", "residue", "cauchy"],
    "常微分方程": ["ordinary differential", "ode", "differential equation"],
    "统计推断": ["statistics", "hypothesis", "estimation", "confidence", "sampling"],
    "泛函分析": ["functional analysis", "normed", "banach", "hilbert", "operator"],
    "线性回归": ["regression", "least squares"],
    "偏微分方程": ["partial differential", "pde", "laplace", "heat equation", "wave equation"],
    "高等代数": ["linear algebra", "matrix", "vector space", "determinant", "eigenvalue"],
    "运筹学": ["optimization", "operations research", "linear programming", "network flow"],
    "数学分析": ["calculus", "limit", "series", "sequence", "continuity", "derivative"],
    "拓扑学": ["topology", "topological", "homeomorph"],
}

# 科缺口 fallback（该科题量不足时从相邻科补）
FALLBACK = {
    "数值分析": ["数学分析", "高等代数"],
    "随机过程": ["概率论", "数学分析"],
    "统计推断": ["概率论", "数值分析", "数学分析"],
    "线性回归": ["统计推断", "概率论"],
    "泛函分析": ["数学分析", "测度积分", "拓扑学"],
    "偏微分方程": ["常微分方程", "数学分析"],
    "常微分方程": ["数学分析", "测度积分"],
    "运筹学": ["离散数学", "概率论"],
    "拓扑学": ["数学分析", "抽象代数"],
    "微分几何": ["数学分析", "测度积分"],
    "非基础及进阶": ["数学分析", "抽象代数"],
}

# 难度带（Omni-MATH difficulty 字段）
DIFF_BANDS = {"easy": (1.0, 3.5), "medium": (4.0, 5.5), "hard": (6.0, 9.5)}
BAND_WEIGHT = {"easy": 0.30, "medium": 0.45, "hard": 0.25}


def load_omni():
    """加载 Omni-MATH，返回 [(key, dict)]。"""
    if not _OMNI.exists():
        print(f"ERROR: {_OMNI} 不存在，请先下载 Omni-MATH")
        sys.exit(1)
    items = []
    with open(_OMNI, encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            d["key"] = f"omni:{i}"
            items.append(d)
    return items


def classify(domain_text):
    """Omni-MATH domain → 官方科目（无映射返回 None）。domain 可能为 str 或 list。"""
    if isinstance(domain_text, list):
        domain_text = " ".join(str(x) for x in domain_text)
    dt = (domain_text or "").lower()
    for subject, kws in DOMAIN_MAP.items():
        for kw in kws:
            if kw in dt:
                return subject
    return None


def band_of(diff):
    for name, (lo, hi) in DIFF_BANDS.items():
        try:
            v = float(diff)
        except (TypeError, ValueError):
            continue
        if lo <= v <= hi:
            return name
    return None


def state_path(set_id):
    return _SCHED / f"set{set_id}_state.json"


def load_state(set_id):
    p = state_path(set_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def save_state(set_id, st):
    state_path(set_id).write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def round_file(set_id, round_no):
    return _SCHED / f"set{set_id}_round_{round_no:03d}.jsonl"


def alloc_by_subject(n):
    """按官方分布分配 n 个名额（floor + 余数按权重大小补）。"""
    total_w = sum(OFFICIAL_DIST.values())
    alloc = {}
    for subj, w in OFFICIAL_DIST.items():
        alloc[subj] = n * w // total_w
    remain = n - sum(alloc.values())
    order = sorted(OFFICIAL_DIST, key=lambda s: -OFFICIAL_DIST[s])
    for i in range(remain):
        alloc[order[i % len(order)]] += 1
    return alloc


def pick_subject_questions(items_by_key, pool_keys, subject, k, rng, exclude=()):
    """从指定科目的未用题中按难度带抽 k 题；不足时返回缺口数与已选题。"""
    chosen = []
    exclude = set(exclude)
    need = k
    # 按难度带配额
    band_quota = {b: max(1, round(need * w)) for b, w in BAND_WEIGHT.items()}
    # 修正配额和为 need
    diff_sum = sum(band_quota.values())
    band_quota["medium"] += need - diff_sum
    for band in ("easy", "medium", "hard"):
        lo, hi = DIFF_BANDS[band]
        cands = [key for key in pool_keys
                 if key not in exclude
                 and classify(items_by_key[key].get("domain", "")) == subject
                 and (lambda v: v is not None and lo <= v <= hi)(_num(items_by_key[key].get("difficulty")))]
        rng.shuffle(cands)
        take = min(band_quota[band], len(cands))
        chosen.extend(cands[:take])
        exclude.update(cands[:take])
    # 缺口：同科不限难度补
    if len(chosen) < need:
        cands = [key for key in pool_keys
                 if key not in exclude
                 and classify(items_by_key[key].get("domain", "")) == subject]
        rng.shuffle(cands)
        chosen.extend(cands[: need - len(chosen)])
        exclude.update(cands[: need - len(chosen)])
    return chosen, max(0, need - len(chosen))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_rule_judgeable_set():
    """加载规则可判定题集合（V4.1：Omni-MATH-Rule 闭式答案子集，按 problem 匹配）。

    仅标注"答案形态是闭式"（调度元数据），答案内容由调度器/判分使用，
    求解器 state 不含 answer 字段，合规无泄漏。
    """
    rule_path = _OMNI.parent / "omni_math_rule.jsonl"
    if not rule_path.exists():
        print("  [info] omni_math_rule.jsonl 不存在，rule_judgeable 标注跳过")
        return None
    s = set()
    with open(rule_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            p = str(d.get("problem") or "").strip()
            if p:
                s.add(p)
    return s


def next_round(set_id, state, items_by_key, rng, overlap_ok=False):
    """生成下一轮 112 题：错题优先 + 新题按官方分布补足。"""
    # 错题优先（按错轮次旧→新）
    wrong = sorted(state.get("wrong", []), key=lambda w: w["round"])
    wrong_keys = [w["key"] for w in wrong if w["key"] in items_by_key]
    used = set(state.get("used", []))
    # 跨套互斥：另一套测试用过的题本套不再抽（除非 overlap_ok）
    other = 2 if set_id == 1 else 1
    other_state = load_state(other)
    if not overlap_ok and other_state:
        used |= set(other_state.get("used", []))
    # 可用的新题池（排除 used；overlap_ok 时允许与另一套重叠）
    pool_keys = [key for key in items_by_key if key not in used]
    n_new = ROUND_SIZE - len(wrong_keys)
    picked = list(wrong_keys)
    picked_set = set(picked)
    gaps = {}
    if n_new > 0:
        alloc = alloc_by_subject(n_new)
        for subject, k in alloc.items():
            if k <= 0:
                continue
            chosen, gap = pick_subject_questions(items_by_key, pool_keys, subject, k, rng,
                                                 exclude=picked_set)
            picked.extend(chosen)
            picked_set.update(chosen)
            if gap > 0:
                gaps[subject] = gap
        # 科缺口用 fallback 科补
        for subject, gap in gaps.items():
            for fb in FALLBACK.get(subject, []):
                if gap <= 0:
                    break
                chosen, g2 = pick_subject_questions(items_by_key, pool_keys, fb, gap, rng,
                                                    exclude=picked_set)
                picked.extend(chosen)
                picked_set.update(chosen)
                gap = g2
            if gap > 0:
                print(f"  [warn] 科目 {subject} 缺口 {gap} 题无 fallback 可补")
    # 仍不足 112：从剩余池任意补（错题+缺口极端情况）
    if len(picked) < ROUND_SIZE:
        rest = [key for key in pool_keys if key not in picked_set]
        rng.shuffle(rest)
        picked.extend(rest[: ROUND_SIZE - len(picked)])
    picked = picked[:ROUND_SIZE]
    # 最终去重（理论不应有重复，防御性）
    seen = set()
    picked = [k for k in picked if not (k in seen or seen.add(k))]
    if len(picked) < ROUND_SIZE:
        rest = [key for key in items_by_key if key not in seen]
        rng.shuffle(rest)
        picked.extend(rest[: ROUND_SIZE - len(picked)])

    # 输出题组
    round_no = state["round"] + 1
    out_path = round_file(set_id, round_no)
    rule_set = load_rule_judgeable_set()
    # 配额科目记录（fallback 补的题保留配额信息，subject 为实际科目）
    quota_subject = {}
    if n_new > 0:
        alloc = alloc_by_subject(n_new)
        slot = 0
        for subject, k in alloc.items():
            for _ in range(k):
                quota_subject[slot + len(wrong_keys)] = subject  # 新题排在错题之后
                slot += 1
    with open(out_path, "w", encoding="utf-8") as f:
        for i, key in enumerate(picked):
            d = items_by_key[key]
            rec = {
                "idx": i,
                "key": key,
                "subject": classify(d.get("domain", "")) or "其他",
                "quota_subject": quota_subject.get(i, ""),
                "domain": d.get("domain", ""),
                "difficulty": d.get("difficulty"),
                "source": d.get("source", ""),
                "problem": d.get("problem", ""),
                "answer": d.get("answer", ""),
                # V4.1：规则可判定（答案形态 = 闭式数值/简单表达式）标注——
                # 求解端据此走"代码精确计算 + 独立方法双验证"确定性路线。
                "rule_judgeable": bool(rule_set) and d.get("problem", "").strip() in rule_set,
                "round": round_no,
                "retest": key in wrong_keys,  # 是否为错题重测
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[set{set_id}] 第 {round_no} 轮已生成: {out_path}")
    print(f"  错题重测 {len(wrong_keys)} 题 | 新题 {len(picked) - len(wrong_keys)} 题")
    if gaps:
        print(f"  科缺口: {gaps}")
    # 状态推进：本轮题立即计入 used（跨套互斥依赖此记账；submit 时幂等去重）
    prev_used = set(state.get("used", []))
    state.setdefault("used", []).extend(k for k in picked if k not in prev_used)
    state["pending_round"] = round_no
    save_state(set_id, state)
    return out_path


def submit_results(set_id, round_no, results_path, items_by_key):
    """回填判分结果：correct 移出错题本；wrong/partial 留本；error 不计。"""
    st = load_state(set_id)
    if st is None:
        print(f"ERROR: set{set_id} 未初始化")
        sys.exit(1)
    # 幂等守卫：该轮已提交过则拒绝重复回填
    if any(r.get("round") == round_no for r in st.get("rounds", [])):
        print(f"[set{set_id}] 第 {round_no} 轮已提交过，跳过（防重复计数）")
        stats(set_id)
        return
    if st.get("pending_round") != round_no:
        print(f"WARN: pending_round={st.get('pending_round')} != {round_no}，仍按 {round_no} 处理")
    wrong_by_key = {w["key"]: w for w in st.get("wrong", [])}
    cnt = Counter()
    detail = []
    with open(results_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            key = r.get("key")
            verdict = str(r.get("verdict", "")).lower()
            cnt[verdict] += 1
            if key in wrong_by_key:
                w = wrong_by_key[key]
                if verdict in ("correct", "partial"):
                    # 做对（partial 视为接近，按用户规则"直到正确"→ partial 仍留本）
                    if verdict == "correct":
                        st["wrong"] = [x for x in st["wrong"] if x["key"] != key]
                        st.setdefault("done", []).append(key)
                else:
                    w["last_round"] = round_no
            else:
                if verdict in ("wrong", "partial"):
                    st.setdefault("wrong", []).append(
                        {"key": key, "round": round_no, "last_round": round_no})
            detail.append({"key": key, "verdict": verdict})
    # 本轮全部题计入 used
    round_keys = set(detail_key["key"] for detail_key in detail)
    st.setdefault("used", []).extend(sorted(round_keys - set(st.get("used", []))))
    # 记录轮次统计
    st.setdefault("rounds", []).append({
        "round": round_no,
        "correct": cnt["correct"],
        "partial": cnt["partial"],
        "wrong": cnt["wrong"],
        "error": cnt["error"],
        "n": sum(cnt.values()),
    })
    st["round"] = round_no
    st.pop("pending_round", None)
    save_state(set_id, st)
    # 存结果副本
    copy_path = _SCHED / f"set{set_id}_results_{round_no:03d}.jsonl"
    with open(copy_path, "w", encoding="utf-8") as f:
        for d_ in detail:
            f.write(json.dumps(d_, ensure_ascii=False) + "\n")
    print(f"[set{set_id}] 第 {round_no} 轮回填完成: correct={cnt['correct']} "
          f"partial={cnt['partial']} wrong={cnt['wrong']} error={cnt['error']}")
    print(f"  错题本剩余 {len(st.get('wrong', []))} 题")
    stats(set_id)


def stats(set_id):
    st = load_state(set_id)
    if st is None:
        print(f"set{set_id}: 未初始化")
        return
    print(f"\n=== set{set_id} (version={st.get('version')}, seed={st.get('seed')}) ===")
    print(f"已完成轮次: {st.get('round', 0)} | 错题本: {len(st.get('wrong', []))} 题 | "
          f"已做对: {len(st.get('done', []))} 题 | 已用: {len(st.get('used', []))} 题")
    for r in st.get("rounds", []):
        print(f"  轮 {r['round']}: n={r['n']} correct={r['correct']} partial={r['partial']} "
              f"wrong={r['wrong']} error={r['error']}")


def init_set(set_id, version, seed, items_by_key, rng):
    st = {
        "set": set_id,
        "version": version,
        "seed": seed,
        "round": 0,
        "pending_round": None,
        "used": [],
        "wrong": [],
        "done": [],
        "rounds": [],
    }
    save_state(set_id, st)
    print(f"[set{set_id}] 初始化完成 (version={version}, seed={seed})，生成首轮…")
    next_round(set_id, st, items_by_key, rng)


def main():
    parser = argparse.ArgumentParser(description="112 题/轮测试调度器（错题重测 + 双套版本）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--set", type=int, required=True)
    p_init.add_argument("--version", default="main")
    p_init.add_argument("--seed", type=int, default=1001)

    p_next = sub.add_parser("next")
    p_next.add_argument("--set", type=int, required=True)

    p_sub = sub.add_parser("submit")
    p_sub.add_argument("--set", type=int, required=True)
    p_sub.add_argument("--round", type=int, required=True)
    p_sub.add_argument("--results", required=True)

    p_stats = sub.add_parser("stats")
    p_stats.add_argument("--set", type=int, required=True)

    args = parser.parse_args()

    items = load_omni()
    items_by_key = {d["key"]: d for d in items}
    print(f"题库加载: {len(items)} 题 (Omni-MATH)")

    if args.cmd == "init":
        rng = random.Random(args.seed)
        init_set(args.set, args.version, args.seed, items_by_key, rng)
    elif args.cmd == "next":
        st = load_state(args.set)
        if st is None:
            print(f"ERROR: set{args.set} 未初始化，先执行 init")
            sys.exit(1)
        rng = random.Random(st["seed"] * 1000 + st["round"] + 1)
        next_round(args.set, st, items_by_key, rng)
    elif args.cmd == "submit":
        submit_results(args.set, args.round, args.results, items_by_key)
    elif args.cmd == "stats":
        stats(args.set)


if __name__ == "__main__":
    main()
