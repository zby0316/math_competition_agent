"""Build a fixed, text-only U-MATH diagnostic set without calling an LLM."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import requests


REVISION = "7210f97b3f21122c3a90126935e966ca7d8951b6"
SHARD = "data/test-00000-of-00001.parquet"
SHARD_SHA256 = "9cc4042a4b61a148f4b28c02ffb3bfc11114937e31d9d6ead190f18e0f9da074"
SEED = "math-agent-umath-text24-v1:"
SUBJECTS = (
    "algebra",
    "differential_calc",
    "integral_calc",
    "multivariable_calculus",
    "precalculus_review",
    "sequences_series",
)
DEFAULT_OUTPUT = Path(__file__).with_name("umath_text24.jsonl")


def _load_shard(local_path: Path | None) -> bytes:
    if local_path is not None:
        data = local_path.read_bytes()
    else:
        session = requests.Session()
        # Some local proxies cannot reach Hugging Face even when direct access works.
        session.trust_env = False
        urls = (
            f"https://huggingface.co/datasets/toloka/u-math/resolve/{REVISION}/{SHARD}",
            f"https://hf-mirror.com/datasets/toloka/u-math/resolve/{REVISION}/{SHARD}",
        )
        errors = []
        data = None
        for url in urls:
            try:
                response = session.get(url, timeout=(8, 45))
                response.raise_for_status()
                data = response.content
                break
            except requests.RequestException as exc:
                errors.append(f"{url}: {exc}")
        if data is None:
            raise RuntimeError("U-MATH shard download failed:\n" + "\n".join(errors))

    digest = hashlib.sha256(data).hexdigest()
    if digest != SHARD_SHA256:
        raise ValueError(f"U-MATH shard SHA-256 mismatch: {digest}")
    return data


def build_records(data: bytes) -> list[dict]:
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError("Install the benchmark-only dependency: pip install pyarrow") from exc

    import pyarrow as pa

    table = parquet.read_table(
        pa.BufferReader(data),
        columns=["uuid", "subject", "has_image", "problem_statement", "golden_answer"],
    )
    rows = table.to_pylist()
    records = []
    for subject in SUBJECTS:
        pool = [
            row for row in rows
            if row["subject"] == subject
            and row["has_image"] is False
            and row["problem_statement"]
            and row["golden_answer"]
        ]
        if len(pool) < 4:
            raise ValueError(f"Too few text-only U-MATH questions for {subject}: {len(pool)}")
        pool.sort(key=lambda row: hashlib.sha256(
            (SEED + row["uuid"]).encode("utf-8")
        ).hexdigest())
        for row in pool[:4]:
            records.append({
                "idx": len(records),
                "problem": row["problem_statement"],
                "answer": row["golden_answer"],
                # main.py forwards `subject` as agent metadata, so use a distinct
                # name to keep the benchmark's topic label out of the prompt.
                "source_subject": subject,
                "source_id": row["uuid"],
                "source_revision": REVISION,
            })
    if len({record["source_id"] for record in records}) != 24:
        raise ValueError("The selected questions contain duplicate source IDs")
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-parquet", type=Path, help="Use a local copy of the pinned shard")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    records = build_records(_load_shard(args.source_parquet))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    print(f"Wrote {len(records)} questions to {args.output}")


if __name__ == "__main__":
    main()
