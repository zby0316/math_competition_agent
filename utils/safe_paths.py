"""Constrain evaluator-controlled identifiers to direct files under an output root."""
from pathlib import Path


def safe_problem_idx(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("idx must be a non-negative integer")
    return value


def output_file(root: Path, idx, suffix: str) -> Path:
    """Return one direct child of *root* or fail closed before any write."""
    number = safe_problem_idx(idx)
    base = Path(root).resolve()
    candidate = (base / f"{number}{suffix}").resolve()
    if candidate.parent != base:
        raise ValueError("output path escapes its directory")
    return candidate
