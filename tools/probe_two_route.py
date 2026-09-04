"""双路一致性 A/B 探针（本地真实 API，带标签题集）。

对每个非客观题（computation/proof）跑两次 reason_step：
  * SECOND_ROUTE=0 —— 基线（今天线上行为）
  * SECOND_ROUTE=1 —— 双路（一致保首轮；分歧仲裁）
把每次产出的答案与 expected 用 AnswerCompare 判分，输出"改进/回退/持平"表。

用法（项目根目录）：
    python tools/probe_two_route.py --dataset <json|jsonl> [--idx 0,7,14] [--concurrency 2]

题集格式（与 eval_local 兼容）：每行 JSON 或 JSON 数组，字段 idx/problem/answer。
默认指向竞品 awa 的带标签 bank（只作离线测量，绝不入库/入仓/出厂）。
"""
import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from eval_local import LocalClient, grade, load_env  # noqa: E402


def load_items(path: str):
    text = Path(path).read_text(encoding="utf-8")
    if path.endswith(".jsonl"):
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        rows = json.loads(text)
        if isinstance(rows, dict):
            rows = rows.get("data", rows.get("problems", []))
    return rows


def classify_once(client, problem: str):
    """跑一次真实分类器节点，拿到 category + question_mode（与线上同一入口）。"""
    from utils.deps import Deps
    from utils.score_budget import ScoreBudget
    from utils.domain_loader import DomainLoader

    deps = Deps(client=client, skills_loader=DomainLoader(), mcp_client=None,
                token_budget=ScoreBudget(), time_budget=None,
                logger=logging.getLogger("probe"))
    from nodes.classify_domain import classify_domain
    state = {"problem": problem, "idx": 0}
    out = classify_domain(state, {"configurable": {"deps": deps}})
    return out.get("category", "数学分析"), out.get("question_mode", "computation")


def solve_once(client, problem: str, category: str, mode: str, second_route: bool) -> dict:
    os.environ["SECOND_ROUTE"] = "1" if second_route else "0"
    from utils.deps import Deps
    from utils.domain_loader import DomainLoader
    from utils.score_budget import ScoreBudget
    from utils.wall_clock import TimeBudget
    from nodes.reason_step import reason_step

    deps = Deps(client=client, skills_loader=DomainLoader(), mcp_client=None,
                token_budget=ScoreBudget(), time_budget=TimeBudget(),
                logger=logging.getLogger("probe"))
    out = reason_step({"problem": problem, "category": category,
                       "question_mode": mode},
                      {"configurable": {"deps": deps}})
    rr = out.get("reasoning_result") or {}
    trace = out.get("reasoning_trace") or []
    second = [t for t in trace if t.get("status") == "second_route"]
    return {
        "answer": rr.get("answer", ""),
        "src": rr.get("answer_source", "-"),
        "second_route": second[-1] if second else None,
        "attempts": out.get("reasoning_attempts", 0),
    }


def run_one(idx, problem, expected, client):
    mode0 = None
    try:
        from utils.question_profile import classify_question_mode, is_objective_mode
        mode0 = classify_question_mode(problem)
        if is_objective_mode(mode0):
            return {"idx": idx, "skip": f"objective({mode0})"}
        category, mode = classify_once(client, problem)
        if not mode:
            from utils.question_profile import classify_question_mode
            mode = classify_question_mode(problem)
        base = solve_once(client, problem, category, mode, second_route=False)
        two = solve_once(client, problem, category, mode, second_route=True)
        base_ok, base_reason = grade(problem, base["answer"], expected)
        two_ok, two_reason = grade(problem, two["answer"], expected)
        return {
            "idx": idx, "mode": mode, "category": category,
            "base_ok": bool(base_ok), "two_ok": bool(two_ok),
            "verdict": "improved" if (not base_ok and two_ok)
                       else ("regressed" if (base_ok and not two_ok) else "same"),
            "base_reason": base_reason, "two_reason": two_reason,
            "second_route": two["second_route"],
            "expected": str(expected)[:120],
        }
    except Exception as exc:  # noqa: BLE001 - 单题失败不中断整批
        return {"idx": idx, "error": f"{type(exc).__name__}: {exc}"[:300]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, help="带标签题集 json/jsonl")
    parser.add_argument("--idx", default="", help="逗号分隔 idx（默认取非客观题等距 12 题）")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--out", default="tools/probe_two_route_results.json")
    args = parser.parse_args()

    env = load_env()
    client = LocalClient(env)
    items = load_items(args.dataset)
    wanted = [int(x) for x in args.idx.split(",")] if args.idx else None
    rows = [it for it in items if wanted is None or int(it.get("idx", -1)) in wanted]
    if not rows:
        raise SystemExit(f"题集为空：--dataset={args.dataset} idx={args.idx}")
    print(f"探针题数: {len(rows)} | concurrency={args.concurrency} | 每题为基线+双路两次 reason_step", flush=True)

    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(run_one, int(it.get("idx", i)), it["problem"], it["answer"], client): it
                   for i, it in enumerate(rows)}
        done = 0
        for fut in as_completed(futures):
            done += 1
            res = fut.result()
            results.append(res)
            tag = res.get("verdict", res.get("skip", res.get("error", "?")))
            line = f"[{done}/{len(rows)}] idx={res.get('idx')} mode={res.get('mode','-')} => {tag}"
            if res.get("verdict") == "improved":
                line += f" (基错→双对)"
            elif res.get("verdict") == "regressed":
                line += f" (基对→双错 ⚠)"
            print(line, flush=True)

    results.sort(key=lambda r: r.get("idx", 0))
    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    scored = [r for r in results if "verdict" in r]
    n_imp = sum(1 for r in scored if r["verdict"] == "improved")
    n_reg = sum(1 for r in scored if r["verdict"] == "regressed")
    n_same = len(scored) - n_imp - n_reg
    base_acc = sum(1 for r in scored if r["base_ok"]) / len(scored) if scored else 0
    two_acc = sum(1 for r in scored if r["two_ok"]) / len(scored) if scored else 0
    print(f"\n=== 结果 ({time.time()-t0:.0f}s) ===")
    print(f"基线正确率: {base_acc:.2%} | 双路正确率: {two_acc:.2%} | "
          f"改进 {n_imp} / 回退 {n_reg} / 持平 {n_same}（共 {len(scored)} 题可判）")
    if n_reg == 0 and n_imp > 0:
        print("=> 双路零回退且净改进")
    elif n_reg > n_imp:
        print("=> 双路净回退，不建议开启")
    else:
        print("=> 双路净收益不明显，谨慎决策")
    print(f"明细写入 {args.out}")


if __name__ == "__main__":
    main()
