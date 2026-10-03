"""Fresh-context cached proof audit. Default preflight makes zero API requests."""
import argparse
import json
import os
import subprocess
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config import CONFIG
from benchmarks.prepare_proof_audit_v2 import source_paths
from utils.proof_audit import AUDIT_VERSION, audit_messages, audit_once
from utils.run_manifest import atomic_json, ensure_manifest, make_manifest, sha256


def load_env(path):
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key.strip().startswith("INTERN_"):
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_pack(pack, *, root=ROOT):
    manifest = json.loads((pack / "pack.manifest.json").read_text(encoding="utf-8"))
    if manifest["audit_version"] != AUDIT_VERSION or manifest["config"] != CONFIG:
        raise ValueError("Audit version or configuration changed")
    raw = (pack / "input.jsonl").read_bytes()
    if sha256(raw) != manifest["input_sha256"]:
        raise ValueError("Frozen inputs changed")
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    if any(set(row) != {"idx", "problem", "final_response"} or type(row["idx"]) is not int for row in rows):
        raise ValueError("Only idx, original problem and final public answer are permitted")
    if [row["idx"] for row in rows] != manifest["indices"] or len({r["idx"] for r in rows}) != len(rows):
        raise ValueError("Frozen indices changed")
    for row in rows:
        if sha256(row["final_response"].encode()) != manifest["answer_sha256"][str(row["idx"])]:
            raise ValueError("Frozen public answer changed")
        prompt = (pack / "prompts" / f"{row['idx']}.json").read_bytes()
        if (sha256(prompt) != manifest["prompt_sha256"][str(row["idx"])] or
                json.loads(prompt) != audit_messages(row["problem"], row["final_response"])):
            raise ValueError("Frozen prompt changed")
    archive = pack / "source.zip"
    if sha256(archive.read_bytes()) != manifest["audit_source_archive_sha256"]:
        raise ValueError("Frozen source archive changed")
    if {p.relative_to(root).as_posix() for p in source_paths(root)} != set(manifest["source_files"]):
        raise ValueError("Current source file list differs from frozen source")
    with zipfile.ZipFile(archive) as zipped:
        if set(zipped.namelist()) != set(manifest["source_files"]):
            raise ValueError("Source archive file list changed")
        for name, digest in manifest["source_files"].items():
            path = (root / name).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError("Source path outside workspace")
            if sha256(zipped.read(name)) != digest or not path.is_file() or sha256(path.read_bytes()) != digest:
                raise ValueError(f"Current source differs from frozen source: {name}")
    return rows, manifest


def ledger_count(path):
    if not path.exists():
        return 0
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if any(set(entry) != {"attempt", "idx", "admitted_at"} or entry["attempt"] != i + 1
           for i, entry in enumerate(entries)):
        raise ValueError("Unknown or corrupt historical HTTP admissions; resume refused")
    return len(entries)


def install_durable_http_cap(ledger, idx, cap):
    """Serial-worker requests.post admissions persist BEFORE sending, including failures.

    A crash between journaling and sending may overcount. It can never grant an
    extra request. This ledger measures admitted attempts, not server receipts.
    """
    import requests
    from utils.call_recording import HttpBudgetExceeded
    original = requests.post
    def post(*args, **kwargs):
        count = ledger_count(ledger)
        if count >= cap:
            raise HttpBudgetExceeded("Audit HTTP admission cap exhausted; no request sent")
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"attempt": count + 1, "idx": idx, "admitted_at": time.time()}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return original(*args, **kwargs)
    requests.post = post


def worker(row, output, cap):
    from llm_client import InternChatClient
    from utils.call_recording import RecordedClient
    from utils.score_budget import ScoreBudget
    from utils.wall_clock import TimeBudget
    # Same transport tuning as the approved solver runs, without constructing an agent.
    client = InternChatClient(retry=1, timeout=1300)
    ledger = output / "http.admissions.jsonl"
    install_durable_http_cap(ledger, row["idx"], cap)
    probe = SimpleNamespace(count=lambda: ledger_count(ledger))
    class AuditRecordedClient(RecordedClient):
        def chat(self, *args, **kwargs):
            before = probe.count()
            try:
                return super().chat(*args, **kwargs)
            finally:
                # Denied admissions are not physical HTTP attempts.
                client._tls.http_attempts = probe.count() - before
    bound = AuditRecordedClient(client, output / "calls", row["idx"], probe)
    budget = TimeBudget()
    started = time.monotonic()
    result = audit_once(bound, row["problem"], row["final_response"], time_budget=budget,
                        token_budget=ScoreBudget())
    atomic_json(output / f"{row['idx']}.json", {"idx": row["idx"], **result,
                "elapsed_s": time.monotonic() - started, "completed": True})


def run_case(row, args, *, run_process=subprocess.run):
    """Kill a stalled worker before starting another; no late concurrent audits."""
    destination = args.output_dir / f"{row['idx']}.json"
    atomic_json(destination, {"idx": row["idx"], "final_response": row["final_response"],
                              "status": "started", "completed": False,
                              "mathematical_correctness_verified": False})
    started = time.monotonic()
    ceiling = min(CONFIG["node_timeouts"]["coordinator"], CONFIG["problem_time_budget_s"])
    command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--pack", str(args.pack.resolve()),
               "--output-dir", str(args.output_dir.resolve()), "--env-file", str(args.env_file.resolve()),
               "--max-http-attempts", str(args.max_http_attempts), "--worker-idx", str(row["idx"]), "--run"]
    try:
        process = run_process(command, timeout=ceiling, capture_output=True, text=True, encoding="utf-8")
        existing = json.loads(destination.read_text(encoding="utf-8"))
        if process.returncode or not existing.get("completed"):
            atomic_json(destination, {**existing, "status": "worker_failed", "completed": True,
                "worker_exit_code": process.returncode, "error": process.stderr,
                "elapsed_s": time.monotonic() - started})
    except subprocess.TimeoutExpired:
        # subprocess.run kills and waits for the child. Already admitted HTTP is
        # charged even if the server's result remains unobservable after killing.
        atomic_json(destination, {"idx": row["idx"], "final_response": row["final_response"],
            "status": "audit_timeout", "completed": True, "elapsed_s": time.monotonic() - started,
            "mathematical_correctness_verified": False, "server_completion_unknown": True})
    except OSError as exc:
        atomic_json(destination, {"idx": row["idx"], "final_response": row["final_response"],
            "status": "worker_launch_failed", "completed": True, "error": str(exc),
            "elapsed_s": time.monotonic() - started, "mathematical_correctness_verified": False})


def validate_history(output):
    admissions = ledger_count(output / "http.admissions.jsonl")
    observed = 0
    for path in (output / "calls").glob("*.calls.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            attempts = json.loads(line).get("http_attempts")
            if type(attempts) is not int or attempts < 0:
                raise ValueError("Unknown historical HTTP attempts; resume refused")
            observed += attempts
    if observed > admissions:
        raise ValueError("HTTP journal missing historical admissions; resume refused")
    return admissions


def save_summary(output, rows):
    results = [json.loads((output / f"{r['idx']}.json").read_text(encoding="utf-8"))
               for r in rows if (output / f"{r['idx']}.json").exists()]
    calls = [json.loads(line) for path in (output / "calls").glob("*.calls.jsonl")
             for line in path.read_text(encoding="utf-8").splitlines()]
    usage = {key: 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    unknown = 0
    for call in calls:
        item = call.get("usage")
        if not isinstance(item, dict) or not all(type(item.get(k)) is int for k in usage):
            unknown += 1
            continue
        for key in usage:
            usage[key] += item[key]
    admissions = validate_history(output)
    observed = sum(c["http_attempts"] for c in calls)
    atomic_json(output / "summary.json", {"status": "audit_run_finished_pending_manual_review",
        "planned_audits": len(rows), "audit_jobs_completed": sum(bool(r.get("completed")) for r in results),
        "model_transport_calls_recorded": len(calls), "http_attempts_observed_in_completed_calls": observed,
        "http_attempts_admitted": admissions, "admitted_without_completed_call_record": admissions - observed,
        "usage_known_sum": usage, "usage_unknown_calls": unknown,
        "stops": dict(Counter(c.get("finish_reason") or "unknown" for c in calls)),
        "result_statuses": dict(Counter(r["status"] for r in results)),
        "elapsed_s": sum(r.get("elapsed_s", 0) for r in results),
        "mathematical_correctness_verified": False, "manual_review_required": True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--max-http-attempts", type=int, default=8)
    parser.add_argument("--worker-idx", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.max_http_attempts < 1:
        parser.error("Positive HTTP cap required")
    rows, pack_manifest = load_pack(args.pack)
    load_env(args.env_file)
    from llm_client import DEFAULT_API_BASE
    model = os.environ.get("INTERN_MODEL", CONFIG["model"])
    api_base = os.environ.get("INTERN_API_BASE", DEFAULT_API_BASE)
    if not args.run:
        print(json.dumps({"status": "dry_run_no_api", "indices": [r["idx"] for r in rows],
            "model": model, "api_base": api_base, "concurrency": 1,
            "audit_version": AUDIT_VERSION, "input_sha256": pack_manifest["input_sha256"],
            "source_archive_sha256": pack_manifest["audit_source_archive_sha256"],
            "max_http_attempts": args.max_http_attempts,
            "temperature": CONFIG["temperatures"]["coordinator"],
            "max_tokens": CONFIG["max_tokens"]["coordinator"],
            "transport_outer_retries": CONFIG["llm_max_retries"], "transport_internal_retries": 1,
            "problem_time_budget_s": CONFIG["problem_time_budget_s"],
            "audit_worker_ceiling_s": CONFIG["node_timeouts"]["coordinator"],
            "output_dir": str(args.output_dir.resolve())}, ensure_ascii=False, indent=2))
        return
    if model != CONFIG["model"] or api_base != DEFAULT_API_BASE:
        raise ValueError("Model or endpoint differs from approved baseline; new proposal required")
    client_info = SimpleNamespace(model=model, api_base=api_base, timeout=1300, retry=1)
    manifest = make_manifest(args.pack / "input.jsonl", rows, client_info, 1)
    manifest.update(code=pack_manifest["audit_code"], audit_version=AUDIT_VERSION,
                    pack_manifest_sha256=sha256((args.pack / "pack.manifest.json").read_bytes()),
                    max_http_attempts=args.max_http_attempts, worker_ceiling_s=CONFIG["node_timeouts"]["coordinator"])
    ensure_manifest(args.output_dir / "run.manifest.json", manifest,
                    has_results=args.output_dir.exists() and any(args.output_dir.iterdir()))
    validate_history(args.output_dir)
    if args.worker_idx is not None:
        row = next(r for r in rows if r["idx"] == args.worker_idx)
        pending = json.loads((args.output_dir / f"{row['idx']}.json").read_text(encoding="utf-8"))
        if pending.get("status") != "started" or pending.get("completed"):
            raise ValueError("Worker requires a newly admitted parent audit job")
        worker(row, args.output_dir, args.max_http_attempts)
        return
    for row in rows:
        path = args.output_dir / f"{row['idx']}.json"
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
            if not previous.get("completed"):
                raise ValueError("Interrupted worker requires manual reconciliation before resume")
            if previous.get("final_response") != row["final_response"]:
                raise ValueError("Cached public answer differs from frozen input")
            continue  # Failures/truncation are terminal too; no semantic retry.
        if ledger_count(args.output_dir / "http.admissions.jsonl") >= args.max_http_attempts:
            print("HTTP cap exhausted; remaining audits were not started", flush=True)
            break
        run_case(row, args)
        print(f"Finished audit idx={row['idx']}", flush=True)
    save_summary(args.output_dir, rows)


if __name__ == "__main__":
    main()
