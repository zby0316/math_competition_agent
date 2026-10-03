"""Local run identity and atomic artifacts; never record credentials."""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from config import AGENT_VERSION, CONFIG

ROOT = Path(__file__).resolve().parents[1]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def code_identity(root: Path = ROOT) -> dict:
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)
    try:
        if Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve() != root.resolve():
            raise OSError("Archived code is not the enclosing repository")
        commit = git("rev-parse", "HEAD").decode().strip()
        difference = git("diff", "HEAD", "--binary")
        untracked = {}
        for name in git("ls-files", "--others", "--exclude-standard", "-z").decode("utf-8").split("\0"):
            if name and (root / name).is_file():
                untracked[name] = sha256((root / name).read_bytes())
        digest = sha256(difference + json.dumps(untracked, sort_keys=True).encode())
        return {"commit": commit, "dirty": bool(difference or untracked), "worktree_sha256": digest}
    except (OSError, subprocess.SubprocessError, UnicodeError):
        source = {}
        for path in sorted(root.rglob("*")):
            if path.is_file() and not any(part in {".git", "__pycache__", ".pytest_cache", ".venv", "outputs", "logs"}
                                          for part in path.relative_to(root).parts):
                if path.name.startswith(".env"):
                    continue
                source[path.relative_to(root).as_posix()] = sha256(path.read_bytes())
        return {"commit": None, "dirty": None,
                "worktree_sha256": sha256(json.dumps(source, sort_keys=True).encode())}


def make_manifest(dataset: Path, inputs: list[dict], client, concurrency: int) -> dict:
    return {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "code": code_identity(), "agent_version": AGENT_VERSION,
        "model": getattr(client, "model", None), "api_base": getattr(client, "api_base", None),
        "config": CONFIG, "client_timeout": getattr(client, "timeout", None),
        "client_retries": getattr(client, "retry", None), "concurrency": concurrency,
        "dataset_sha256": sha256(dataset.read_bytes()),
        "input_sha256": sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode()),
        "inputs": inputs, "unknown_means": "not observable; not zero",
    }


def ensure_manifest(path: Path, manifest: dict, has_results: bool = False) -> None:
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        identity_keys = set(manifest) - {"created_at"}
        if any(previous.get(key) != manifest[key] for key in identity_keys):
            raise ValueError("Run identity changed; use a new output path instead of mixing results")
    elif has_results:
        raise ValueError("Existing results lack a run manifest; use a new output path")
    else:
        atomic_json(path, manifest)
