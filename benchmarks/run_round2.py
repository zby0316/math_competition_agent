"""Version-isolated paired-run harness. Default is dry-run; --run calls the API."""
import argparse
import importlib.util
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

HARNESS_ROOT = Path(__file__).resolve().parents[1]


def load_harness_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, HARNESS_ROOT / "utils" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_env(path):
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key.startswith("INTERN_"):
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def selected_identity(code_root, manifest_tools):
    code_root = code_root.resolve()
    identity = manifest_tools.code_identity(code_root)
    provenance = code_root.parent / "baseline_archive.json"
    if provenance.is_file():
        data = json.loads(provenance.read_text(encoding="utf-8"))
        if Path(data["code_root"]).resolve() == code_root:
            archive = code_root.with_suffix(".zip")
            if manifest_tools.sha256(archive.read_bytes()) != data["archive_sha256"]:
                raise ValueError("Baseline archive hash changed")
            with zipfile.ZipFile(archive) as zipped:
                expected = {name for name in zipped.namelist() if not name.endswith("/")}
                for name in expected:
                    if (code_root / name).read_bytes() != zipped.read(name):
                        raise ValueError("Baseline code differs from the archived commit")
                actual = {path.relative_to(code_root).as_posix() for path in code_root.rglob("*")
                          if path.is_file() and not any(part in {"__pycache__", ".pytest_cache", "outputs", "logs"}
                                                      for part in path.relative_to(code_root).parts)}
                if actual != expected:
                    raise ValueError("Baseline has extra or missing source files")
            identity.update(commit=data["commit"], dirty=False, archive_sha256=data["archive_sha256"])
    return identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-root", type=Path, default=HARNESS_ROOT)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=HARNESS_ROOT / ".env")
    parser.add_argument("--run", action="store_true", help="Explicitly make API calls after batch approval")
    parser.add_argument("--max-http-attempts", type=int, default=20,
                        help="Hard cap per output directory, including retries and resumed attempts (default 20)")
    args = parser.parse_args()
    if args.max_http_attempts < 1:
        parser.error("--max-http-attempts must be positive")
    inputs = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines()]
    if any(set(row) != {"idx", "problem"} or not isinstance(row["idx"], int) for row in inputs):
        raise ValueError("Input must contain only integer idx and problem; references/labels are excluded")
    if len({row["idx"] for row in inputs}) != len(inputs):
        raise ValueError("Duplicate idx")
    # Every invocation is a separate interpreter: config, prompts, knowledge,
    # graph and client are all imported from the selected code snapshot.
    sys.path.insert(0, str(args.code_root.resolve()))
    from config import CONFIG
    manifest_tools = load_harness_module("round2_manifest", "run_manifest.py")
    recorder_tools = load_harness_module("round2_recording", "call_recording.py")
    load_env(args.env_file)
    model = os.environ.get("INTERN_MODEL", CONFIG["model"])
    if not args.run:
        print(json.dumps({"status": "dry_run_no_api", "code_root": str(args.code_root.resolve()),
                          "code": selected_identity(args.code_root, manifest_tools),
                          "model": model, "concurrency": 1, "items": len(inputs),
                          "max_http_attempts": args.max_http_attempts,
                          "indices": [row["idx"] for row in inputs],
                          "input_sha256": manifest_tools.sha256(args.input.read_bytes()),
                          "output_dir": str(args.output_dir)}, ensure_ascii=False, indent=2))
        return
    from llm_client import InternChatClient
    from user_agent import ReasoningAgent
    client = InternChatClient()
    # Match the transport tuning done by both versions' ReasoningAgent.
    agent = ReasoningAgent(client)
    manifest = manifest_tools.make_manifest(args.input, inputs, client, 1)
    manifest["code"] = selected_identity(args.code_root, manifest_tools)
    manifest["harness_code"] = manifest_tools.code_identity(HARNESS_ROOT)
    manifest["harness_version"] = 1
    manifest["max_http_attempts"] = args.max_http_attempts
    manifest_tools.ensure_manifest(args.output_dir / "run.manifest.json", manifest,
                                  has_results=any(path.name != "run.manifest.json" for path in args.output_dir.glob("*.json")))
    spent = 0
    for path in (args.output_dir / "calls").glob("*.calls.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            attempts = json.loads(line).get("http_attempts")
            if not isinstance(attempts, int):
                raise ValueError("Cannot safely resume a capped run with unknown historical HTTP attempts")
            spent += attempts
    http_probe = recorder_tools.install_http_probe(max(0, args.max_http_attempts - spent))
    for row in inputs:
        if http_probe.exhausted():
            print("HTTP budget exhausted; remaining questions were not run", flush=True)
            break
        destination = args.output_dir / f"{row['idx']}.json"
        if destination.exists():
            existing = json.loads(destination.read_text(encoding="utf-8"))
            if existing.get("final_response") and existing.get("status") == "success":
                continue
        bound = recorder_tools.RecordedClient(client, args.output_dir / "calls", row["idx"], http_probe)
        # Baseline lacks the optional per-problem hook; the same adapter is bound
        # directly to a fresh agent for both versions without editing either tree.
        agent = ReasoningAgent(bound)
        started = time.monotonic()
        result = agent.solve(row["problem"], {"idx": row["idx"]})
        status = "error" if any(entry.get("step") == "error" for entry in result.get("trace", [])) else "success"
        manifest_tools.atomic_json(destination, {"idx": row["idx"], "status": status,
            "budget_exhausted": http_probe.exhausted(),
            "elapsed_s": time.monotonic() - started, **result})
        print(f"Finished idx={row['idx']}", flush=True)


if __name__ == "__main__":
    main()
