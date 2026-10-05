"""Version-isolated paired-run harness. Default is dry-run; --run calls the API."""
import argparse
import atexit
import importlib.util
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

HARNESS_ROOT = Path(__file__).resolve().parents[1]

# The harness dynamically imports its recording helper. Keep the repository
# root importable so the helper may use shared ``utils`` modules.
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))


class _RunLease:
    """An OS-level nonblocking lease for one resumable output directory.

    The HTTP cap is reconstructed from append-only call logs.  Two concurrent
    resumptions can otherwise read the same historical count, both believe the
    remaining quota is available, and jointly exceed the cap.  This byte-range
    lock is released automatically if a process is killed, unlike a sentinel
    file that can become stale after an interrupted desktop session.
    """

    def __init__(self, handle):
        self._handle = handle
        self._released = False

    def release(self):
        if self._released:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._released = True


def acquire_run_lease(output_dir: Path) -> _RunLease:
    """Acquire the exclusive live-run lease or fail before any API call."""
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / ".active_run.lock"
    handle = lock_path.open("a+b")
    if lock_path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError(
            f"An active run already owns {output_dir}; refusing concurrent resume before API calls"
        ) from exc
    return _RunLease(handle)


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
    provenance, data = None, None
    for name in ("baseline_archive.json", "candidate_archive.json"):
        possible = code_root.parent / name
        if not possible.is_file():
            continue
        candidate_data = json.loads(possible.read_text(encoding="utf-8"))
        if Path(candidate_data.get("code_root", "")).resolve() == code_root:
            provenance, data = possible, candidate_data
            break
    if provenance is not None:
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
    # Hold the lease from manifest validation through call-log accounting and
    # the final atomic result write.  Registering cleanup covers normal errors;
    # OS byte locks are also released if the host terminates this process.
    lease = acquire_run_lease(args.output_dir)
    atexit.register(lease.release)
    from llm_client import InternChatClient
    from user_agent import ReasoningAgent
    # A hard HTTP budget is meaningful only if one admitted chat cannot make
    # hidden transport retries.  Keep retries at one; graph-level recovery is
    # separately visible and budgeted by the recording wrapper below.
    client = InternChatClient(retry=1)
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
    remaining_attempts = max(0, args.max_http_attempts - spent)
    http_probe = recorder_tools.install_http_probe(remaining_attempts)
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
            "http_admitted": http_probe.admitted(),
            "http_transport_attempts": http_probe.transport_attempts(),
            "elapsed_s": time.monotonic() - started, **result})
        print(f"Finished idx={row['idx']}", flush=True)


if __name__ == "__main__":
    main()
