from typing import Annotated, Any, Dict, List, Optional
import operator
from typing_extensions import NotRequired, TypedDict

from utils.problem_anchor import make_problem_anchor
from utils.problem_profile import classify_question_mode, mode_from_metadata

class MathAgentState(TypedDict):
    # 输入阶段（必需）
    problem: str
    metadata: Dict[str, Any]
    idx: int
    problem_anchor: NotRequired[Dict[str, Any]]
    problem_integrity_events: NotRequired[Annotated[List[Dict], operator.add]]
    reasoning_attempts: int
    python_attempts: int
    errors: Annotated[List[Dict], operator.add]
    # 分类阶段
    question_mode: NotRequired[str]
    category: NotRequired[str]
    category_confidence: NotRequired[float]
    candidate_categories: NotRequired[List[str]]
    classification_stages_used: NotRequired[List[str]]
    # 难度画像（VeritasMath：分类 prefill 顺带输出，驱动软预算与双路径触发）
    difficulty: NotRequired[str]            # easy | medium | hard
    difficulty_confidence: NotRequired[float]
    # 推理阶段
    reasoning_result: NotRequired[Optional[Dict[str, Any]]]
    reasoning_trace: NotRequired[List[Dict]]
    reasoning_retry_hint: NotRequired[Optional[str]]
    reasoning_raw_response: NotRequired[str]
    # 第二推理路径（异构方法；首轮高风险领域或调解"换方法"触发）
    reasoning_result_alt: NotRequired[Optional[Dict[str, Any]]]
    reasoning_trace_alt: NotRequired[List[Dict]]
    reasoning_raw_response_alt: NotRequired[str]
    method_variant: NotRequired[str]        # "" | "B"，由 Send 扇出携带
    # Python 验证阶段
    python_code: NotRequired[str]
    python_output: NotRequired[Optional[Dict[str, Any]]]
    python_trace: NotRequired[List[Dict]]
    python_retry_hint: NotRequired[Optional[str]]
    python_evidence_status: NotRequired[str]
    python_evidence_summary: NotRequired[str]
    python_contradictions: NotRequired[List[str]]
    # 验证阶段
    validation_status: NotRequired[str]
    validation_details: NotRequired[Dict[str, Any]]
    validated_answer: NotRequired[str]
    validation_history: NotRequired[Annotated[List[Dict], operator.add]]
    # 协调阶段
    reconciliation_trace: NotRequired[List[Dict]]
    reconciliation_round: NotRequired[int]
    # 语义仲裁阶段（只选择既有候选，不生成新数学答案）
    semantic_arbiter_status: NotRequired[str]
    semantic_arbiter_decision: NotRequired[str]
    semantic_arbiter_trace: NotRequired[List[Dict]]
    semantic_arbiter_attempts: NotRequired[int]
    answer_locked: NotRequired[bool]
    # 过程审计阶段（VeritasMath critic：契约完整性 + 关键计算抽核）
    critic_status: NotRequired[str]         # pass | incomplete | skipped | error
    critic_missing: NotRequired[List[str]]
    critic_trace: NotRequired[List[Dict]]
    critic_rounds: NotRequired[int]
    # 确定性复算季后赛（VeritasMath playoff：候选冲突时的代回复算裁决）
    playoff_status: NotRequired[str]        # decisive | indecisive | skipped | error
    playoff_answer: NotRequired[str]
    playoff_trace: NotRequired[List[Dict]]
    # 客观题复核投票（低置信时追加一次独立采样比对）
    objective_revote_trace: NotRequired[List[Dict]]
    # 模结构守护命中标记（F_2 / Z_m 语境；提示注入与静态核查记录）
    modular_guard: NotRequired[Dict[str, Any]]
    # 定向修复提示来源标记（critic/playoff → reconciliation 直通用，一次性消费）
    repair_hint_source: NotRequired[Optional[str]]
    # 输出阶段
    final_response: NotRequired[str]
    coordination_detail: NotRequired[Optional[str]]  # coordinator 原始解题说明，记入 trace（计算题 final_response 已收敛为简洁答案）
    fallback_source: NotRequired[str]
    # 内部控制
    branch_hint: NotRequired[Optional[str]]  # Send() 扇出携带的重试提示（solving 子图内）
    next_node: NotRequired[Optional[str]]
    should_terminate: NotRequired[bool]
    token_budget_consumed: NotRequired[int]
    # 各节点实测耗时。必须是 reducer 字段：solving 子图内 reasoning_agent 与
    # python_agent 并行扇出，两条分支会同时写入。
    node_timings: NotRequired[Annotated[List[Dict], operator.add]]

def create_initial_state(problem: str, metadata: Dict[str, Any]) -> MathAgentState:
    metadata = metadata if isinstance(metadata, dict) else {}
    question_mode = mode_from_metadata(metadata) or classify_question_mode(problem)
    return MathAgentState(
        problem=problem, metadata=metadata, idx=metadata.get("idx", -1),
        problem_anchor=make_problem_anchor(problem, metadata.get("idx", -1)),
        problem_integrity_events=[],
        question_mode=question_mode,
        reasoning_attempts=0, python_attempts=0, semantic_arbiter_attempts=0,
        answer_locked=False, errors=[], validation_history=[],
    )
