"""Compare saved runs offline; proof correctness remains a separate human review."""
import argparse
import json
import statistics
from pathlib import Path


def summarize(directory):
    manifest = json.loads((directory / "run.manifest.json").read_text(encoding="utf-8"))
    rows = []
    for item in manifest["inputs"]:
        idx = item["idx"]
        path = directory / f"{idx}.json"
        if not path.exists():
            rows.append({"idx": idx, "status": "missing", "review": "unreviewed"})
            continue
        output = json.loads(path.read_text(encoding="utf-8"))
        call_path = directory / "calls" / f"{idx}.calls.jsonl"
        calls = [json.loads(line) for line in call_path.read_text(encoding="utf-8").splitlines()] if call_path.exists() else None
        trace = output.get("trace", [])
        diagnostics = next((row for row in trace if row.get("step") == "diagnostics"), {})
        counts = [call.get("http_attempts") for call in calls] if calls is not None else None
        rows.append({"idx": idx, "status": output.get("status"), "review": "unreviewed",
                     "elapsed_s": output.get("elapsed_s"), "final_response": output.get("final_response"),
                     "solve_success": bool(output.get("final_response")) and not any(row.get("step") == "error" for row in trace),
                     "logical_model_calls": len(calls) if calls is not None else diagnostics.get("logical_model_calls"),
                     "http_attempts": sum(counts) if counts is not None and all(isinstance(n, int) for n in counts) else None,
                     "python": diagnostics.get("python"),
                     "truncated_responses": sum(call.get("finish_reason") == "length" for call in calls) if calls is not None else None})
    times = [row["elapsed_s"] for row in rows if isinstance(row.get("elapsed_s"), (int, float))]
    return {"manifest": manifest, "items": rows, "summary": {
        "planned_items": len(rows), "available_items": len(times),
        "median_elapsed_s": statistics.median(times) if times else None,
        "max_elapsed_s": max(times) if times else None,
        "official_score_inference": None, "proof_reviews_completed": 0}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    baseline, candidate = summarize(args.baseline), summarize(args.candidate)
    keys = ("input_sha256", "model", "api_base", "config", "concurrency", "harness_version")
    mismatches = [key for key in keys if baseline["manifest"].get(key) != candidate["manifest"].get(key)]
    if mismatches:
        raise ValueError(f"Runs are not comparable: {mismatches}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"status": "awaiting_full_answer_review", "baseline": baseline,
                                    "candidate": candidate}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved paired outputs for manual review to {args.out}")


if __name__ == "__main__":
    main()
