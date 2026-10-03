"""Freeze public answers and fresh audit messages without making model requests."""
import argparse
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config import CONFIG
from utils.proof_audit import AUDIT_VERSION, audit_messages
from utils.run_manifest import atomic_json, code_identity, sha256

INDICES = [4, 5, 0, 3, 10]


def source_paths(root):
    return sorted(path for path in root.rglob("*") if path.is_file()
        and not any(part in {".git", ".venv", "outputs", "logs", "__pycache__", ".pytest_cache"}
                    for part in path.relative_to(root).parts)
        and (path.suffix == ".py" or (path.suffix == ".md" and "knowledge" in path.relative_to(root).parts)
             or path.name in {"requirements.txt", "pytest.ini"}))


def prepare(source_run, manual_review, out, *, root=ROOT):
    if out.exists() and any(out.iterdir()):
        raise ValueError("Preparation directory must be empty; do not replace frozen material")
    source = json.loads((source_run / "run.manifest.json").read_text(encoding="utf-8"))
    reviewed = json.loads(manual_review.read_text(encoding="utf-8"))
    problems = {row["idx"]: row["problem"] for row in source["inputs"]}
    archive = source_run / "tested_source.zip"
    source_archive_sha = sha256(archive.read_bytes())
    tested = json.loads((source_run / "tested_source.manifest.json").read_text(encoding="utf-8"))
    if tested["source_archive_sha256"] != source_archive_sha or tested["code"] != source["code"]:
        raise ValueError("Source archive does not match tested provenance")
    if source["config"] != CONFIG or source["model"] != CONFIG["model"]:
        raise ValueError("Source configuration differs from approved settings")
    provenance = {"run_manifest_sha256": sha256((source_run / "run.manifest.json").read_bytes()),
                  "source_code": source["code"], "source_archive_sha256": source_archive_sha,
                  "source_agent_version": source["agent_version"], "source_output_sha256": {}}
    inputs, manual = [], {"source_review_sha256": sha256(manual_review.read_bytes()), "items": {}}
    for idx in INDICES:
        path = source_run / f"{idx}.json"
        answer = json.loads(path.read_text(encoding="utf-8"))
        record = reviewed["items"][str(idx)]
        final = answer["final_response"]
        if answer["status"] != "success" or sha256(final.encode()) != record["final_response_sha256"]:
            raise ValueError(f"Unverified or changed source answer: {idx}")
        provenance["source_output_sha256"][str(idx)] = sha256(path.read_bytes())
        inputs.append({"idx": idx, "problem": problems[idx], "final_response": final})
        manual["items"][str(idx)] = record
    out.mkdir(parents=True, exist_ok=True)
    input_path = out / "input.jsonl"
    input_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in inputs), encoding="utf-8")
    prompts = {}
    for row in inputs:
        path = out / "prompts" / f"{row['idx']}.json"
        atomic_json(path, audit_messages(row["problem"], row["final_response"]))
        prompts[str(row["idx"])] = sha256(path.read_bytes())
    # Deliberately separate; the runtime never reads this file.
    atomic_json(out / "manual_only.json", manual)
    paths = source_paths(root)
    files = {p.relative_to(root).as_posix(): sha256(p.read_bytes()) for p in paths}
    zip_path = out / "source.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for path in paths:
            entry = zipfile.ZipInfo(path.relative_to(root).as_posix(), date_time=(2026, 10, 3, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            zipped.writestr(entry, path.read_bytes())
    manifest = {"schema_version": 1, "audit_version": AUDIT_VERSION, "indices": INDICES,
                "input_sha256": sha256(input_path.read_bytes()), "prompt_sha256": prompts,
                "answer_sha256": {str(r["idx"]): sha256(r["final_response"].encode()) for r in inputs},
                "source": provenance, "audit_code": code_identity(root), "config": CONFIG,
                "source_files": files, "audit_source_archive_sha256": sha256(zip_path.read_bytes()),
                "manual_labels_in_messages": False, "default_workflow_modified": False}
    atomic_json(out / "pack.manifest.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, default=ROOT / "outputs/round2_B_repair6")
    parser.add_argument("--manual-review", type=Path, default=ROOT / "outputs/round2_B_repair6_manual_review.json")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    manifest = prepare(args.source_run, args.manual_review, args.out)
    print(json.dumps({"status": "prepared_no_api", "indices": INDICES,
                      "input_sha256": manifest["input_sha256"],
                      "audit_source_archive_sha256": manifest["audit_source_archive_sha256"]}, indent=2))


if __name__ == "__main__":
    main()
