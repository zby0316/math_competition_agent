# -*- coding: utf-8 -*-
"""学习圈（study circle）：计算题多学生隔离参考求解 + 证据选择/裁判辩论。

移植自 multiface polyhedron_core/study_circle.py（同族管道，72.32 分仓）。
原则：独立学生不看任何参考（保留无锚定基线）；其余学生各持不同参考视图；
答案一致时按确定性证据评分选择；分歧时低温度证据裁判介入——**禁止多数票**，
所有候选都错时必须重新计算，裁判可产出全新答案。

成本：学生并行（~230s）+ 裁判（~160s）≈ 现有单次推理调用墙钟，不额外拉长
官方 6h 总预算。安全设计：config 默认关（enabled=False = 69.64 原版行为）；
无可答候选返回 None 由 reason_step 走原路径；时间紧张（fast_path）时
由调用方跳过。

仅用于计算题首轮推理：hint 驱动的复核重试仍走原调解路径，不进课堂。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from config import CONFIG
from utils.classroom_evidence import order_by_evidence
from utils.deps import get_deps
from utils.domain_excerpt import _score, _split_modules, _terms
from utils.llm_retry_guard import chat_with_retry
from utils.prompt_bank import REASONING_PROMPT
from utils.question_profile import (
    answer_coverage_clause,
    mode_instruction,
    structure_instruction,
)
from utils.score_budget import estimate_tokens


def _answer_key(value: str) -> str:
    return re.sub(r"[\s$\\{}，,。.;；:：]+", "", str(value or "")).casefold()


def _top_excerpts(skill_doc: str, problem: str, limits=(2400, 1600)) -> list[str]:
    """从领域技能文档里取与当前题最相关的两个模块（互不重叠的参考视图）。"""
    head, modules = _split_modules(skill_doc)
    if not modules:
        return [head[: limits[0]]] if head.strip() else []
    problem_terms = _terms(problem)
    ranked = sorted(modules, key=lambda m: _score(m, problem_terms), reverse=True)
    excerpts = []
    for index, limit in enumerate(limits):
        if index < len(ranked) and ranked[index].strip():
            excerpts.append(ranked[index].strip()[:limit])
    return excerpts


def _build_assignments(state: dict[str, Any], skill_doc: str) -> list[dict[str, Any]]:
    excerpts = _top_excerpts(skill_doc, state["problem"])
    max_students = int(CONFIG.get("study_circle", {}).get("max_students", 3) or 3)
    assignments: list[dict[str, Any]] = [{
        "role": "independent_student",
        "reference_rank": None,
        "reference": None,
        "instruction": "不查看任何参考材料，独立建立模型并求解；明确关键假设、边界条件和最终答案。",
    }]
    role_names = ["lead_student"] + [f"peer_student_{i}" for i in range(1, max_students)]
    for role, excerpt in zip(role_names, excerpts):
        assignments.append({
            "role": role,
            "reference_rank": None,
            "reference": {"problem": "", "solution": excerpt, "relation": "related",
                          "source_dataset": "skill_doc"},
            "instruction": "参考材料只作为方法线索。先说明适用条件，再从当前题条件独立推导，"
                           "不得无验证复制参考里的结论。",
        })
    return assignments


def _role_prompt(state: dict[str, Any], assignment: dict[str, Any], skill_doc: str) -> str:
    reference = assignment.get("reference") or {}
    reference_block = ""
    if reference.get("solution"):
        reference_block = ("\n\n[参考材料-技能模块]（只作为方法线索，参数与当前题不同，"
                           "必须重新计算）:\n" + str(reference["solution"]))
    prompt = REASONING_PROMPT.format(
        category=state["category"],
        skill_document=skill_doc[:1200] + reference_block,
        problem=state["problem"],
    )
    prompt += mode_instruction(state.get("question_mode", "computation"))
    prompt += structure_instruction(state["problem"])
    prompt += answer_coverage_clause(state["problem"])
    prompt += (
        "\n\n[课堂身份] " + assignment["role"] + "。" + assignment["instruction"] +
        "\n必须明确写出：参考与当前题的条件差异；哪些步骤可迁移；哪些量必须重新计算。"
        "无参考时明确写'独立推导'。最终仍按四章节格式输出。"
    )
    return prompt


def _solve_one(state, assignment, deps) -> dict[str, Any] | None:
    from nodes.reason_step import _parse_reasoning_output

    cfg = CONFIG.get("study_circle", {})
    prompt = _role_prompt(state, assignment,
                          deps.skills_loader.get_skill_document(state["category"]))
    try:
        response = chat_with_retry(
            deps.client,
            messages=[{"role": "user", "content": prompt}],
            temperature=float(cfg.get("student_temperature", 0.75)),
            max_tokens=int(cfg.get("student_max_tokens", 16384)),
            logger=deps.logger,
            time_budget=deps.time_budget,
            expected_call_seconds=float(cfg.get("student_expected_s", 230)),
            label=f"classroom_{assignment['role']}",
        )
    except Exception as exc:  # noqa: BLE001
        deps.logger.warning("Classroom student %s failed: %s", assignment["role"], exc)
        return None
    if deps.token_budget:
        deps.token_budget.consume(estimate_tokens(prompt), estimate_tokens(response))
    parsed = _parse_reasoning_output(response,
                                     question_mode=state.get("question_mode", "computation"))
    reference = assignment.get("reference") or {}
    return {
        "role": assignment["role"],
        "answer": parsed.get("answer", ""),
        "analysis": parsed.get("analysis", ""),
        "steps": parsed.get("steps", []),
        "validation_points": parsed.get("validation_points", []),
        "raw_response": response,
        "reference_rank": assignment.get("reference_rank"),
        "reference_relation": reference.get("relation", "none"),
        "reference_source": reference.get("source_dataset", ""),
        "independent_of_reference": assignment.get("reference") is None,
        "answer_shape_valid": bool(parsed.get("answer")),
    }


def _debate_prompt(problem: str, candidates: list[dict[str, Any]]) -> str:
    blocks = []
    for candidate in candidates:
        steps = "\n".join(
            str(step.get("description", "")) for step in candidate.get("steps", [])[:5]
        )
        blocks.append(
            f"### {candidate['role']}\n答案：{candidate['answer']}\n"
            f"分析：{candidate['analysis'][:1400]}\n步骤：{steps[:1800]}\n"
            f"参考关系：{candidate['reference_relation']}；来源：{candidate['reference_source']}"
        )
    return (
        "你是多面体课堂的证据裁判。候选来自彼此隔离的学生。禁止按多数票决定。"
        "逐一检查题面条件、所求对象、参数替换、代回、边界、反例和答案类型。"
        "若所有候选都错，必须重新计算。只采用能由当前题证明的结论。"
        "按'## 问题分析 / ## 详细解题步骤 / ## 最终答案 / ## 关键验证点'输出。\n\n"
        f"## 当前题\n{problem}\n\n" + "\n\n".join(blocks)
    )


def run_study_circle(state: dict[str, Any], config: dict) -> dict[str, Any] | None:
    """计算题首轮的学习圈推理。返回与 reason_step 相同形状的结果；不适用时 None。"""
    from nodes.reason_step import _parse_reasoning_output

    deps = get_deps(config)
    if not CONFIG.get("study_circle", {}).get("enabled"):
        return None
    if state.get("branch_hint"):
        # 复核重试是对前一轮答案的定向修正，课堂的独立采样会丢掉 hint 的靶向性。
        return None
    skill_doc = deps.skills_loader.get_skill_document(state["category"])
    if not skill_doc or not skill_doc.strip():
        return None
    assignments = _build_assignments(state, skill_doc)
    if len(assignments) < 2:
        return None

    indexed: dict[int, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, len(assignments))) as executor:
        futures = {executor.submit(_solve_one, state, assignment, deps): (index, assignment)
                   for index, assignment in enumerate(assignments)}
        for future in as_completed(futures):
            index, assignment = futures[future]
            try:
                result = future.result()
                if result is not None:
                    indexed[index] = result
            except Exception as exc:  # noqa: BLE001
                failures.append({"role": assignment.get("role"), "error": str(exc)[:240]})
    candidates = [indexed[i] for i in sorted(indexed)]

    usable = [c for c in candidates if c.get("answer")]
    if not usable:
        return None  # 交给 reason_step 的原推理路径，含其压缩重试兜底。

    keys = {_answer_key(c["answer"]) for c in usable}
    debate_used = len(keys) > 1
    selected = order_by_evidence(usable)[0]
    debate_raw = ""
    if debate_used and not (deps.time_budget and deps.time_budget.fast_path()):
        cfg = CONFIG.get("study_circle", {})
        try:
            debate_raw = chat_with_retry(
                deps.client,
                messages=[{"role": "user", "content": _debate_prompt(state["problem"], usable)}],
                temperature=float(cfg.get("debate_temperature", 0.2)),
                max_tokens=int(cfg.get("debate_max_tokens", 8192)),
                logger=deps.logger,
                time_budget=deps.time_budget,
                expected_call_seconds=float(cfg.get("debate_expected_s", 160)),
                label="classroom_evidence_debate",
            )
            if deps.token_budget:
                deps.token_budget.consume(
                    estimate_tokens(_debate_prompt(state["problem"], usable)),
                    estimate_tokens(debate_raw))
            debated = _parse_reasoning_output(
                debate_raw, question_mode=state.get("question_mode", "computation"))
            if debated.get("answer"):
                selected = {
                    **selected,
                    "role": "evidence_judge",
                    "answer": debated["answer"],
                    "analysis": debated.get("analysis", ""),
                    "steps": debated.get("steps", []),
                    "validation_points": debated.get("validation_points", []),
                    "raw_response": debate_raw,
                }
        except Exception as exc:  # noqa: BLE001
            failures.append({"role": "evidence_judge", "error": str(exc)[:240]})

    reasoning_result = {
        "analysis": selected.get("analysis", ""),
        "steps": selected.get("steps", []),
        "answer": selected.get("answer", ""),
        "validation_points": selected.get("validation_points", []),
        "answer_source": "classroom_" + selected.get("role", "student"),
    }
    return {
        "reasoning_result": reasoning_result,
        "reasoning_raw_response": selected.get("raw_response", ""),
        "reasoning_attempts": len(candidates) + int(debate_used),
        "reasoning_trace": [{
            "status": "classroom_complete",
            "students": len(candidates),
            "usable_answers": len(usable),
            "distinct_answers": len(keys),
            "debate_used": debate_used,
            "selected_role": selected.get("role"),
            "failures": failures,
        }],
    }
