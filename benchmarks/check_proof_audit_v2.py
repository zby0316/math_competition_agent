"""Offline handoff checks on frozen public answers; model and HTTP calls are mocked."""
import argparse
import json
import re
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.run_proof_audit_v2 import load_pack
from utils.call_recording import RecordedClient
from utils.proof_audit import audit_messages, audit_once
from utils.run_manifest import atomic_json, sha256
from utils.score_budget import ScoreBudget
from utils.wall_clock import TimeBudget


def check(pack, source_run, out):
    rows, manifest = load_pack(pack)
    source = json.loads((source_run / "run.manifest.json").read_text(encoding="utf-8"))
    problems = {r["idx"]: r["problem"] for r in source["inputs"]}
    runtime = {}
    with zipfile.ZipFile(source_run / "tested_source.zip") as zipped:
        for name in zipped.namelist():
            if name.endswith(".py") or (name.startswith("knowledge/") and name.endswith(".md")):
                if (ROOT / name).read_bytes() != zipped.read(name):
                    raise ValueError(f"Previously tested source changed: {name}")
                runtime[name] = sha256(zipped.read(name))
    items = []
    for row in rows:
        old = json.loads((source_run / f"{row['idx']}.json").read_text(encoding="utf-8"))
        if old["final_response"] != row["final_response"] or problems[row["idx"]] != row["problem"]:
            raise ValueError("Source answer or original problem changed")
        section, quote = "(无标题)", None
        for line in row["final_response"].splitlines():
            if re.match(r"^#{1,6}\s", line):
                section = line.strip()
            elif line.strip():
                quote = line
                break
        mock_response = json.dumps({"status": "no_issue_found", "issues": [], "checks": [{
            "quote": quote, "section": section, "premises": "模拟前提，仅测交接", "status": "supported",
            "basis": "模拟核算内容，未审查此证明的数学正确性"}]}, ensure_ascii=False)
        class MockClient:
            _tls = SimpleNamespace(finish_reason="stop", usage=None, http_attempts=0)
            def __init__(self):
                self.calls = []
            def chat(self, **kwargs):
                self.calls.append(kwargs)
                return mock_response
        mock = MockClient()
        client = RecordedClient(mock, out / "calls", row["idx"])
        result = audit_once(client, row["problem"], row["final_response"],
                            time_budget=TimeBudget(), token_budget=ScoreBudget())
        assert len(mock.calls) == 1
        assert mock.calls[0]["messages"] == audit_messages(row["problem"], row["final_response"])
        assert result["final_response"] == row["final_response"]
        assert result["audit"]["checks"][0]["record_status"] == "located_unverified"
        assert not result["audit"]["mathematical_correctness_verified"]
        atomic_json(out / f"{row['idx']}.json", {"idx": row["idx"], "mock_only": True, **result})
        items.append({"idx": row["idx"], "answer_sha256": sha256(row["final_response"].encode()),
                      "mock_logical_audits": 1, "actual_http_attempts": 0,
                      "exact_original_problem_and_answer": True, "quote_location_verified": True})
    report = {"status": "offline_handoff_passed_no_api", "items": items,
              "input_sha256": manifest["input_sha256"],
              "audit_source_archive_sha256": manifest["audit_source_archive_sha256"],
              "previously_tested_source_files_unchanged": runtime,
              "new_api_requests": 0, "mathematical_correctness_verified": False,
              "note": "Mock records verify transport, preservation and quote location only; not detection ability."}
    atomic_json(out / "verification.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--source-run", type=Path, default=ROOT / "outputs/round2_B_repair6")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists() and any(args.out.iterdir()):
        raise ValueError("Use a new offline output directory")
    report = check(args.pack, args.source_run, args.out)
    print(json.dumps({"status": report["status"], "items": len(report["items"]), "new_api_requests": 0}))


if __name__ == "__main__":
    main()
