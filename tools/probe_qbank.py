"""公开题库同源命中率探针（离线分析，不调用 API）。

对 test_sets/eval_112.json（本地验证集）逐题检索 qbank 的 top-k 相似题，
并计算题面 bigram Jaccard 相似度（awa 同款同源题判定指标），输出：
  * 相似度分布（top-1 余弦、top-20 最大 Jaccard）；
  * 同源命中估计（Jaccard ≥ 0.5 视为近似同源）；
  * 命中/未命中题目的 idx 列表——命中率即"检索参考"机制收益的诚实上界。

用法：python tools/probe_qbank.py [--top 20] [--jac-threshold 0.5]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import CONFIG  # noqa: E402


def bigram_jaccard(a: str, b: str) -> float:
    def grams(s: str):
        s = " ".join(str(s).split())
        return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--jac-threshold", type=float, default=0.5)
    args = ap.parse_args()

    CONFIG["qbank_enabled"] = True  # 探针内强制开启检索（不影响主流程默认值）
    from utils.qbank import get_qbank
    qb = get_qbank()
    if qb is None:
        raise SystemExit("qbank 不可用：先运行 python tools/build_qbank.py")

    rows = json.loads((ROOT / "test_sets" / "eval_112.json").read_text(encoding="utf-8"))
    hits, misses = [], []
    sim1, simk = [], []
    for row in rows:
        problem = row.get("problem", "")
        try:
            found = qb.search(problem, top_k=args.top)
        except Exception as exc:  # noqa: BLE001
            print(f"idx {row.get('idx')}: search error {exc}")
            misses.append(row["idx"])
            continue
        if not found:
            misses.append(row["idx"])
            continue
        sim1.append(found[0][0])
        simk.append(max(s for s, _ in found))
        best_jac = max(bigram_jaccard(problem, e["p"]) for _, e in found)
        if best_jac >= args.jac_threshold:
            hits.append((row["idx"], best_jac, found[0][0]))
        else:
            misses.append(row["idx"])
    print(f"共 {len(rows)} 题；top-{args.top} 内同源命中（Jaccard≥{args.jac_threshold}）："
          f"{len(hits)} 题")
    if sim1:
        print(f"top-1 余弦均值 {sum(sim1)/len(sim1):.3f}，top-{args.top} 最大均值 "
              f"{sum(simk)/len(simk):.3f}")
    print("命中:", [(i, round(j, 3)) for i, j, _ in sorted(hits)])
    print("未命中 idx:", sorted(misses))


if __name__ == "__main__":
    main()
