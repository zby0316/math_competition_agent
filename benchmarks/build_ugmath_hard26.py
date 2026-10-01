"""Rebuild the reviewed UGMathBench diagnostic set without model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import requests


REVISION = "8ab16f0c131a9b3b52195e64175cb5a8f3881bbf"
SOURCE_SHA256 = {
    "Abstract_algebra": "fce887a710db5587dcb412a426e50a23c332381e30f2afd24f9f4f447e1938c1",
    "Algebra": "6c3b4464aea639d969e2ba977002c838a34a847d530a838204bd8d03ff9097a1",
    "Calculus_-_multivariable": "26db27564a3c29b54e3fd812bb5424b60dc3e080297f3ec552c43c13e53a5648",
    "Calculus_-_single_variable": "d2188875e7a441732bafab1f7f27d261dadab2202ffa18fd772b32d16e62f443",
    "Combinatorics": "b18614c69a2b1082659c9975bd26d8d02af8c00acce8fada2b2c8bdd92ed29dc",
    "Complex_analysis": "6f12b2a1357198176c4ff241ec79682db50b6ba8dcc143fc4f3030a7bf6afcf6",
    "Differential_equations": "2f3ce51c1c7418bb42d40fe39c6dd392f8dde491b2a51bba73ce48d9c20759b9",
    "Geometry": "3fb5246da55c563c29335edd4a61638fef29e645908b31e70fb10cd5ee615451",
    "Linear_algebra": "9e3869a6a7fed6a621abb02839bdf21e549ea4ad68e1f98b91f0003f7ef38558",
    "Number_theory": "04a4b246cd9c75bc56951b65b691ebb3ce857141e228eb7898602f488ba446e8",
    "Probability": "e2ef48fc7a3a8445419e06be777c5767ebe084c6992c8d89b83508e9289de01c",
    "Set_theory_and_logic": "5b9ef6d75bf8c8754a45f0303adff5cb43bdc5dea28f18fcc5827406f9266887",
    "Statistics": "701e1338be4bd0f3fb8be74672ab0bad3917264a789c6f3ff3c7cbe675a362ea",
    "Trigonometry": "ba162cfc6a69e76abb9a3da6d56aaf7ff9a7e2befa44cfd584bc05e031d8c2cf",
}

# Deliberate selection after reading every source question and reference answer.
# The source's level label is an ordinal difficulty tag, not an official score.
SELECTED_IDS = (
    "Abstract_algebra_0022", "Abstract_algebra_0042",
    "Algebra_0594",
    "Calculus_-_multivariable_0260", "Calculus_-_multivariable_0468",
    "Calculus_-_single_variable_0498", "Calculus_-_single_variable_0711",
    "Combinatorics_0003", "Combinatorics_0078",
    "Complex_analysis_0037", "Complex_analysis_0042",
    "Differential_equations_0234", "Differential_equations_0299",
    "Geometry_0085",
    "Linear_algebra_0366", "Linear_algebra_0463",
    "Number_theory_0033", "Number_theory_0046",
    "Probability_0236", "Probability_0319",
    "Set_theory_and_logic_0031", "Set_theory_and_logic_0050",
    "Statistics_0136", "Statistics_0368",
    "Trigonometry_0123", "Trigonometry_0154",
)

DEFAULT_SOURCE_DIR = Path(__file__).resolve().parents[1] / "outputs" / "ugmathbench_source"
DEFAULT_OUTPUT = Path(__file__).with_name("ugmath_hard26.jsonl")


def load_subject(subject: str, source_dir: Path, download: bool = False) -> list[dict]:
    source_path = source_dir / f"{subject}.json"
    if source_path.exists():
        data = source_path.read_bytes()
    elif download:
        session = requests.Session()
        session.trust_env = False
        urls = (
            f"https://huggingface.co/datasets/UGMathBench/ugmathbench/resolve/{REVISION}/data/{subject}.json",
            f"https://hf-mirror.com/datasets/UGMathBench/ugmathbench/resolve/{REVISION}/data/{subject}.json",
        )
        errors = []
        for url in urls:
            try:
                response = session.get(url, timeout=(8, 60))
                response.raise_for_status()
                data = response.content
                break
            except requests.RequestException as exc:
                errors.append(f"{url}: {exc}")
        else:
            raise RuntimeError("UGMathBench download failed:\n" + "\n".join(errors))
    else:
        raise FileNotFoundError(f"{source_path}; pass --download to fetch the pinned source")

    digest = hashlib.sha256(data).hexdigest()
    if digest != SOURCE_SHA256[subject]:
        raise ValueError(f"UGMathBench {subject} SHA-256 mismatch: {digest}")
    if not source_path.exists():
        source_dir.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(data)
    return json.loads(data)


def build_records(source_dir: Path, download: bool = False) -> list[dict]:
    selected = set(SELECTED_IDS)
    if len(selected) != 26:
        raise ValueError("Duplicate selected source IDs")
    found = {}
    for subject in SOURCE_SHA256:
        for row in load_subject(subject, source_dir, download):
            if row["id"] in selected:
                if row["id"] in found:
                    raise ValueError(f"Duplicate source row: {row['id']}")
                found[row["id"]] = row
    if found.keys() != selected:
        raise ValueError(f"Missing selected IDs: {sorted(selected - found.keys())}")

    records = []
    for source_id in SELECTED_IDS:
        row = found[source_id]
        problem = row["problem_v1"]
        answers = row["answer_v1"]
        answer_types = row["answer_type_v1"]
        answer_options = row["options_v1"]
        if (
            str(row["level"]) not in {"4", "5", "6"}
            or not problem or "\ufffd" in problem
            or not answers or not (len(answers) == len(answer_types) == len(answer_options))
            or problem.count("[ANS]") != len(answers)
        ):
            raise ValueError(f"Malformed selected problem or answer slots: {source_id}")
        records.append({
            "idx": len(records),
            "problem": problem,
            # The existing grader accepts text. Numbered multipart references
            # intentionally require manual review if it cannot align fields.
            "answer": answers[0] if len(answers) == 1 else "\n".join(
                f"{i}. {answer}" for i, answer in enumerate(answers, 1)
            ),
            "answer_slots": answers,
            "answer_types": answer_types,
            "answer_options": answer_options,
            # Use source_* names: main.py forwards plain `subject` to the agent.
            "source_subject": row["subject"],
            "source_topic": row["topic"],
            "source_level": row["level"],
            "source_id": source_id,
            "source_revision": REVISION,
        })
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--download", action="store_true", help="Download missing pinned JSON files")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    records = build_records(args.source_dir, args.download)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    print(f"Wrote {len(records)} questions to {args.output}")


if __name__ == "__main__":
    main()
