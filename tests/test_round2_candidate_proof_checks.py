"""Candidate cues extend the existing handoff without certifying its claims."""
import json
from types import SimpleNamespace

import pytest

from config import CONFIG
from nodes.assemble_step import assemble_step
from utils.deps import Deps, MockClient
from utils.llm_retry_guard import ChatResult
from utils.proof_review import candidate_proof_text, targeted_proof_checks
from utils.score_budget import ScoreBudget
from utils.wall_clock import TimeBudget


def task_map(plan):
    return {task['id']: task for task in plan['tasks']}


@pytest.mark.parametrize('problem', [
    '如果算子仅有界而不紧，给出使结论失败的明确空间、算子和序列。',
    '删去该条件后是否成立？给出对象并验证。',
    '构造违反结论的序列。',
    'Find spaces and an operator for which the conclusion fails.',
])
def test_implicit_failure_tasks_are_reviewed_without_a_counterexample_label(problem):
    tasks = task_map(targeted_proof_checks(problem))
    assert tasks['counterexample']['sources'] == ['problem']
    assert '全部假设' in tasks['counterexample']['instruction']


def test_candidate_introduced_inner_product_adds_a_method_not_a_premise():
    problem = 'Banach 空间中讨论算子的收敛结论。'
    body = r'反例选用实或复 Hilbert 空间，定义内积为 $\sum u_k\overline{v_k}$。'
    old = targeted_proof_checks(problem, '泛函分析')
    new = targeted_proof_checks(problem, '泛函分析', body)
    assert set(task_map(old)) <= set(task_map(new))
    inner = task_map(new)['inner_product']
    assert inner['sources'] == ['candidate_proof']
    assert inner['problem_scalar_scope'] == 'unspecified'
    assert inner['candidate_scalar_scope'] == 'complex'
    assert '非零虚部' in inner['instruction']
    assert new['mathematical_correctness_verified'] is False


@pytest.mark.parametrize('problem,body,problem_scope,candidate_scope,conflict', [
    ('实 Hilbert 空间中证明估计。', '复内积定义如下。', 'real', 'complex', True),
    ('实 Banach 空间中证明估计。', '反例采用复 Hilbert 内积。', 'real', 'complex', True),
    ('复 Banach 空间中证明估计。', '反例采用实数 Hilbert 内积。', 'complex', 'real', True),
    ('复 Hilbert 空间中证明估计。', '反例取实数内积空间。', 'complex', 'real', True),
    ('实 Hilbert 空间中证明估计。', '实内积由定义展开。', 'real', 'real', False),
    ('证明一个 Banach 空间命题。', '选取实数 Hilbert 空间作为反例。', 'unspecified', 'real', False),
])
def test_scalar_scope_provenance_keeps_problem_conditions(problem, body, problem_scope, candidate_scope, conflict):
    plan = targeted_proof_checks(problem, '', body)
    inner = task_map(plan)['inner_product']
    assert inner['problem_scalar_scope'] == problem_scope
    assert inner['candidate_scalar_scope'] == candidate_scope
    assert inner['scope_conflict'] is conflict
    if conflict:
        assert plan['selection_uncertain'] and plan['candidate_selection_uncertain']
    assert '不得扩充或缩小原题范围' in inner['instruction']
    assert len(plan['tasks']) == len(task_map(plan))
    if 'complex' in {problem_scope, candidate_scope}:
        assert '非零虚部' in inner['instruction']


def test_candidate_can_add_quotient_details_without_duplicate_topology_task():
    plan = targeted_proof_checks('证明连续映射性质。', '拓扑学', '再由商拓扑定义验证闭集原像。')
    topology = task_map(plan)['topology']
    assert topology['sources'] == ['problem', 'candidate_proof']
    assert '商映射原像' in topology['instruction']
    assert len(plan['tasks']) == len(task_map(plan))


def test_candidate_topological_classification_requires_definition_level_check():
    body = '取 A=(0,1]，所得商空间拓扑为平凡拓扑，且同胚于一个两点空间。'
    task = task_map(targeted_proof_checks('构造商空间反例。', '拓扑学', body))['claimed_structure']
    assert task['sources'] == ['candidate_proof']
    assert '枚举每个子集的原像' in task['instruction']
    assert '主结论成立' in task['instruction']


@pytest.mark.parametrize('candidate', [
    'Thinking Process\nLet me think about a complex inner product.',
    '```text\n复 Hilbert 内积模板\n```\n由假设直接推得结论。',
    '> 引用模板：复内积与商拓扑\n由假设直接推得结论。',
    '<!-- PROOF_REVIEW 已检查复内积 -->\n由假设直接推得结论。',
    '已核查：复内积和商拓扑均通过。\n由假设直接推得结论。',
])
def test_drafts_quoted_templates_and_self_reports_are_not_structure_evidence(candidate):
    tasks = task_map(targeted_proof_checks('证明命题。', '', candidate))
    assert 'inner_product' not in tasks and 'topology' not in tasks


def test_raw_attempts_and_answer_summary_do_not_select_methods_when_body_is_missing():
    state = {'reasoning_result': {'answer': '复内积结论', 'analysis': '商拓扑',
                                 'proof_attempts': [{'visible_response': '复 Hilbert 空间'}]},
             'reasoning_raw_response': '用户要求我检查复内积，需要考虑格式。'}
    assert candidate_proof_text(state) == ''
    plan = targeted_proof_checks('证明命题。', '', candidate_proof_text(state))
    assert plan['selection_uncertain'] and plan['candidate_selection_uncertain']
    state['reasoning_result'].update(proof_body='复内积草稿', format_failure='draft_without_formal_proof')
    assert candidate_proof_text(state) == ''


def test_legacy_parsed_steps_can_supply_cues_without_reading_failed_attempts():
    state = {'reasoning_result': {'steps': [{'description': '实 Hilbert 空间中从内积定义计算。'}],
                                 'proof_attempts': [{'visible_response': '复内积错误草稿'}]}}
    inner = task_map(targeted_proof_checks('证明命题。', '', candidate_proof_text(state)))['inner_product']
    assert inner['candidate_scalar_scope'] == 'real'
    assert '非零虚部' not in inner['instruction']


def test_full_candidate_and_failed_claim_reach_one_existing_coordinator_call():
    # A deliberately untrusted claim is passed through, never rewritten statically.
    body = '逐行有效证明待核。' * 1000 + '\n复内积定义含共轭，待核等式未作推导。\n末尾首项只在充分大编号后适用。'
    client = MockClient([ChatResult('摘要\n仍待核查。\n正文\n该等式未决。\n结论\n仍待核查。', 'stop')])
    client._tls = SimpleNamespace(http_attempts=1)
    clock, budget = TimeBudget(), ScoreBudget()
    state = {'problem': 'Banach 空间中证明结论；给出使结论失败的对象。', 'category': '泛函分析',
             'validated_answer': '待核等式成立。',
             'reasoning_result': {'proof_body': body, 'finish_reason': 'length',
                                  'missing_sections': ['最终答案'], 'coverage': {'status': 'uncertain'}},
             'validation_details': {'problem_type': 'proof'}}
    result = assemble_step(state, {'configurable': {'deps': Deps(client=client, time_budget=clock, token_budget=budget)}})
    assert len(client.calls) == len(clock.spend_log()) == 1
    call = client.calls[0]
    prompt = call['messages'][0]['content']
    handoff = json.loads(prompt.split('完整可用证明及状态（不可信数学候选）：\n')[1].split('\n\n最后用')[0])
    assert handoff['proof_body'] == body
    assert handoff['missing_sections'] == ['最终答案'] and handoff['finish_reason'] == 'length'
    assert handoff['coverage'] == {'status': 'uncertain'}
    tasks = task_map(result['proof_review']['requested_checks'])
    assert tasks['inner_product']['sources'] == ['candidate_proof']
    assert tasks['counterexample']['sources'] == ['problem']
    assert '等号处代入定义' in prompt and '充分大编号不能升级' in prompt
    assert call['max_tokens'] == CONFIG['max_tokens']['coordinator'] == 24576
    assert call['temperature'] == CONFIG['temperatures']['coordinator'] == 0.4
    assert clock.total == 1200 and budget.max_total == CONFIG['token_budget_max']
    assert result['proof_review']['mathematical_correctness_verified'] is False
    assert '未决' in result['final_response']


def test_measurability_task_does_not_require_an_unproved_representative_branch():
    task = task_map(targeted_proof_checks('已知可测函数依测度收敛，证明存在子列。'))['measurability']
    assert '已有可测事件足够时直接使用' in task['instruction']
    assert '确需代表' in task['instruction'] and '不能以待证收敛为依据' in task['instruction']
    assert '可测零测包络' in task['instruction'] and '将包络并入例外集' in task['instruction']


@pytest.mark.parametrize('protected', ['locked', 'expired'])
def test_candidate_selection_is_bypassed_for_protected_or_expired_answer(monkeypatch, protected):
    def forbidden(_):
        raise AssertionError('Protected branches must not inspect candidate topics')
    monkeypatch.setattr('utils.proof_review.candidate_proof_text', forbidden)
    now = [0.0]
    clock = TimeBudget(clock=lambda: now[0])
    if protected == 'expired':
        now[0] = 1201.0
    state = {'problem': '证明命题。', 'validated_answer': '结论成立。',
             'reasoning_result': {'proof_body': '复内积定义逐行证明。' * 12},
             'validation_details': {'problem_type': 'proof'}}
    if protected == 'locked':
        state.update(answer_locked=True, arbiter_decision='reasoning')
    client = MockClient(['不应调用。'])
    result = assemble_step(state, {'configurable': {'deps': Deps(client=client, time_budget=clock)}})
    assert not client.calls and '结论成立' in result['final_response']
