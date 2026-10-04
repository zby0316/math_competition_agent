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
                # 官方总分记录（平台只给总分，不给逐题日志）：
                #   初赛 69.64/100  —— 2026-09-28 及之前各报告使用的基线
                #   决赛客观评审 85/100 —— 2026-10-04 由用户在决赛榜单确认
                # 注意：这个字段是记录，不是估算，也不与 official_version_verified
                # 冲突（后者表示"分数尚未绑定到某个已验证的代码版本"）。
                # 分数刷新后请同步更新该值与日期。
                "official_score": 85, "official_version_verified": False,
                "api_evaluation_status": "not_run", "model": "intern-s2-preview-397b",
                "needs_before_official_submission": ["approved paired API runs", "strict proof review",
                                                     "freeze candidate commit and platform package requirements"]}
    args.out.with_suffix(".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Built submission draft with {len(files)} files; no model calls or official submission")


if __name__ == "__main__":
    main()
