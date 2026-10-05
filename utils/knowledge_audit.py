"""Offline source-risk inventory for runtime knowledge documents."""
from __future__ import annotations

import json
import re
from pathlib import Path


RISK_PATTERN = re.compile(r"标准答案|正确答案|参考答案重写|官方.{0,12}(?:判|口径|答案)")


def load_catalog(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"entries": []}


def audit_knowledge(knowledge_root: Path) -> list[dict]:
    """Return reproducible line-level risk findings without changing documents."""
    root = Path(knowledge_root)
    catalog = load_catalog(root / "source_catalog.json")
    known = {(entry.get("document"), entry.get("heading")) for entry in catalog.get("entries", [])}
    findings: list[dict] = []
    for path in sorted(root.rglob("*skill.md")):
        relative = path.relative_to(root).as_posix()
        heading = ""
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.startswith("#"):
                heading = line.strip()
            if RISK_PATTERN.search(line):
                findings.append({
                    "document": relative,
                    "line": number,
                    "heading": heading,
                    "text": line.strip(),
                    "catalogued": (relative, heading) in known,
                })
    return findings
