"""Distinguish verification policy from generation and execution failures."""
from collections import Counter


def python_diagnostics(state: dict) -> dict:
    output = state.get("python_output") or {}
    counts = Counter()
    if output.get("skipped"):
        counts["strategy_skipped"] = 1
    for entry in state.get("python_trace") or []:
        kind = entry.get("failure_kind")
        if kind:
            counts[kind] += 1
        elif entry.get("status") == "failed":
            counts["unclassified_failure"] += 1
    if output.get("success"):
        counts["execution_success"] += 1
        if output.get("evidence_status") == "inconclusive":
            counts["evidence_uncertain"] += 1
    return dict(counts)


def call_counts(spend_log: list[dict]) -> dict:
    calls = [entry for entry in spend_log if entry.get("model_call") is True]
    attempts = [entry.get("http_attempts") for entry in calls]
    return {"logical_model_calls": len(calls),
            "http_attempts": sum(attempts) if all(isinstance(n, int) for n in attempts) else None}
