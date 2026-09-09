# -*- coding: utf-8 -*-
"""课堂候选证据评分（移植 multiface polyhedron_core/decision_evidence.py）。

在 LLM 证据裁判之前先用确定性证据特征排序；分歧时才请裁判（禁止多数票，
可重新计算）。各特征在我们的管道里只有部分可得（tool_verified 等由后续
验证节点提供，此处仅用候选携带的字段），不影响排序正确性。
"""

from __future__ import annotations

from typing import Any, Iterable


def evidence_score(candidate: dict[str, Any]) -> float:
    score = 0.0
    score += 3.0 if candidate.get("tool_verified") else 0.0
    score += 2.0 if candidate.get("substitution_verified") else 0.0
    score += 1.5 if candidate.get("covers_all_cases") else 0.0
    score += 1.0 if candidate.get("independent_of_reference") else 0.0
    score += 0.5 if candidate.get("answer_shape_valid") else 0.0
    score -= 2.5 if candidate.get("reference_answer_copied") else 0.0
    score -= 2.0 if candidate.get("counterexample_found") else 0.0
    score -= 1.5 if candidate.get("condition_mismatch") else 0.0
    return score


def order_by_evidence(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = []
    for candidate in candidates:
        item = dict(candidate)
        item["evidence_score"] = evidence_score(item)
        ranked.append(item)
    return sorted(ranked, key=lambda item: (-item["evidence_score"], str(item.get("role", ""))))
