"""Build an offline-reviewable candidate archive without secrets or eval references."""
import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.run_manifest import code_identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/round2_submission_draft.zip")
    args = parser.parse_args()
    paths = [ROOT / name for name in ("user_agent.py", "main.py", "agent_pipeline.py", "config.py",
                                     "llm_client.py", "requirements.txt", "README.md", "LICENSE")]
    for directory in ("nodes", "utils", "graph", "state", "exec_server", "knowledge"):
        paths.extend(path for path in (ROOT / directory).rglob("*") if path.is_file()
                     and path.suffix in {".py", ".md"} and "__pycache__" not in path.parts)
    files = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(paths)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    identity = code_identity()
    with zipfile.ZipFile(args.out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            entry = zipfile.ZipInfo(path.relative_to(ROOT).as_posix(), date_time=(2026, 10, 2, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, path.read_bytes())
    manifest = {"status": "draft_not_submitted", "code": identity, "files": files,
                "archive_sha256": hashlib.sha256(args.out.read_bytes()).hexdigest(),
                "official_score": 85, "official_version_verified": False,
                "api_evaluation_status": "not_run", "model": "intern-s2-preview-397b",
                "needs_before_official_submission": ["approved paired API runs", "strict proof review",
                                                     "freeze candidate commit and platform package requirements"]}
    args.out.with_suffix(".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Built submission draft with {len(files)} files; no model calls or official submission")


if __name__ == "__main__":
    main()
