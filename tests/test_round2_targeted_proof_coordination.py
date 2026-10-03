"""Advisory proof checks and candidate protection, using only simulated calls."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import CONFIG
from nodes.assemble_step import assemble_step
from utils.deps import Deps, MockClient
from utils.llm_retry_guard import ChatResult
from utils.proof_review import extract_review, targeted_proof_checks
from utils.score_budget import ScoreBudget, estimate_tokens
from utils.wall_clock import TimeBudget


@pytest.mark.parametrize('problem,category,expected,excluded,scalar_scope', [
    ('实 Hilbert 空间中证明范数估计。', '泛函分析', {'inner_product'}, {'measurability', 'tails_and_truncation'}, 'real'),
    ('在实或复 Hilbert 空间讨论条件并给反例。', '泛函分析', {'inner_product', 'counterexample'}, {'topology', 'tails_and_truncation'}, 'complex'),
    ('Complex inner product: prove the norm identity.', '高等代数', {'inner_product'}, {'measurability', 'boundaries'}, 'complex'),
    (r'Inner product over $\mathbb{C}$: show the estimate.', '', {'inner_product'}, {'topology'}, 'complex'),
    ('Hilbert space: prove weak convergence.', '泛函分析', {'inner_product'}, {'measurability', 'tails_and_truncation'}, 'unspecified'),
    ('Measurable functions converge almost everywhere.', '数学分析', {'measurability'}, {'inner_product', 'topology'}, None),
    ('证明可测函数尾积分与截断估计。', '测度积分', {'measurability', 'tails_and_truncation'}, {'inner_product'}, None),
    ('用 Baire 证明一致有界并检验完备性。', '泛函分析', {'boundaries'}, {'inner_product', 'tails_and_truncation'}, None),
    ('核对商拓扑开集。', '拓扑学', {'topology'}, {'inner_product', 'measurability'}, None),
])
def test_problem_topics_select_relevant_checks_without_unrelated_work(problem, category, expected, excluded, scalar_scope):
    plan = targeted_proof_checks(problem, category)
    ids = {item['id'] for item in plan['tasks']}
    assert expected <= ids and not ids & excluded
    assert 'premises_and_coverage' in ids
    assert plan['mathematical_correctness_verified'] is False
    if scalar_scope:
        inner = next(task for task in plan['tasks'] if task['id'] == 'inner_product')
        assert inner['scalar_scope'] == scalar_scope
        assert ('非零虚部' in inner['instruction']) is (scalar_scope != 'real')


def test_unrecognized_problem_retains_general_checks_and_uncertainty():
    plan = targeted_proof_checks('证明命题。', '非基础及进阶课程')
    assert [item['id'] for item in plan['tasks']] == ['premises_and_coverage']
    assert plan['selection_uncertain'] is True
    assert '量词顺序' in plan['tasks'][0]['instruction']


def test_full_handoff_and_recalculable_basis_survive_single_budgeted_call():
    old = '未核查的旧摘要。'
    prior = '第一段证明。' * 1300 + '\n候选末尾必须保留。'
    raw_prior = '## 详细解题步骤\n' + prior
    visible = '摘要\n结论需继续核查。\n正文\n必要公式尚无可靠依据，保留其余论证。\n结论\n此项未决。'
    note = {'status': 'none_reported', 'issues': [], 'checks': [
        {'task_id': 'inner_product', 'claim': '待核等式', 'status': 'unresolved', 'basis': '候选未声明内积的线性变量，无法完成两边核算。'}]}
    raw = ChatResult(visible + '\n<!-- PROOF_REVIEW ' + json.dumps(note, ensure_ascii=False) + ' -->', 'length')
    client, budget, clock = MockClient([raw]), ScoreBudget(), TimeBudget()
    client._tls = SimpleNamespace(finish_reason='length', http_attempts=1)
    result = assemble_step({'problem': '复 Hilbert 空间中证明给定等式。', 'category': '泛函分析',
                            'validated_answer': old,
                            'reasoning_result': {'answer': old, 'proof_body': prior, 'finish_reason': 'length',
                                                 'missing_sections': ['最终答案'], 'proof_attempts': [
                                                     {'visible_response': raw_prior, 'finish_reason': 'length'}]},
                            'validation_details': {'problem_type': 'proof'}},
                           {'configurable': {'deps': Deps(client=client, token_budget=budget, time_budget=clock)}})
    assert len(client.calls) == 1
    call = client.calls[0]
    prompt = call['messages'][0]['content']
    assert prior in prompt.replace('\\n', '\n') and 'missing_sections' in prompt
    assert '非零虚部' in prompt and '分别从定义和待核等式计算两边' in prompt
    assert '有效推导或满足全部前提的反例' in prompt
    assert call['max_tokens'] == CONFIG['max_tokens']['coordinator']
    assert call['temperature'] == CONFIG['temperatures']['coordinator']
    assert clock.total == CONFIG['problem_time_budget_s'] == 1200
    assert budget.max_total == CONFIG['token_budget_max']
    assert budget.consumed == estimate_tokens(prompt) + estimate_tokens(raw)
    assert len(clock.spend_log()) == 1 and clock.spend_log()[0]['http_attempts'] == 1
    assert result['final_response'] == visible and old not in result['final_response']
    review = result['proof_review']
    assert review['status'] == 'unresolved' and review['model_reported_status'] == 'none_reported'
    assert review['finish_reason'] == 'length' and review['checks'] == note['checks']
    assert review['mathematical_correctness_verified'] is False
    assert 'inner_product' in {task['id'] for task in review['requested_checks']['tasks']}


def test_unsubstantiated_model_opinion_is_retained_but_never_certified():
    note = {'status': 'issues_corrected', 'issues': ['成员关系'], 'mathematical_correctness_verified': True, 'checks': [
        {'task_id': 'boundaries', 'claim': '成员关系不成立', 'status': 'corrected',
         'basis': '题面未直接给出。'}]}
    visible, review = extract_review('保留原始答卷。\n<!-- PROOF_REVIEW ' + json.dumps(note, ensure_ascii=False) + ' -->')
    assert visible == '保留原始答卷。'
    assert review['checks'] == note['checks']
    assert review['checks_are_model_reports'] is True
    assert review['mathematical_correctness_verified'] is False


def test_missing_or_malformed_optional_checks_do_not_invent_evidence():
    for note in [{'status': 'none_reported', 'issues': []},
                 {'status': 'none_reported', 'checks': '已验证正确'}]:
        _, review = extract_review('正文\n<!-- PROOF_REVIEW ' + json.dumps(note, ensure_ascii=False) + ' -->')
        assert not review.get('checks')
        assert review['mathematical_correctness_verified'] is False


def test_locked_candidate_cannot_be_changed_by_review_opinions():
    candidate = '该映射连续。'
    client = MockClient(['错误审查意见，不应调用。'])
    result = assemble_step({'problem': '证明给定映射连续。', 'category': '拓扑学',
                            'validated_answer': candidate, 'answer_locked': True,
                            'arbiter_decision': 'reasoning',
                            'reasoning_result': {'answer': candidate, 'proof_body': '任取开集，其原像为开集。' * 8},
                            'validation_details': {'problem_type': 'proof'}},
                           {'configurable': {'deps': Deps(client=client)}})
    assert not client.calls and candidate in result['final_response']
    assert result['fallback_source'] == 'validated_answer'


def test_expired_budget_preserves_candidate_without_starting_targeted_call():
    now = [0.0]
    clock = TimeBudget(clock=lambda: now[0])
    now[0] = 1201.0
    client = MockClient(['预算耗尽后不应调用。'])
    result = assemble_step({'problem': '证明给定估计。', 'category': '泛函分析',
                            'validated_answer': '该估计成立。',
                            'reasoning_result': {'proof_body': '由假设和三角不等式逐项估计。' * 8},
                            'validation_details': {'problem_type': 'proof'}},
                           {'configurable': {'deps': Deps(client=client, time_budget=clock)}})
    assert not client.calls
    assert result['final_response'].startswith('结论：该估计成立。')
    assert '证明过程：' in result['final_response']


def test_nonproof_computation_keeps_existing_coordinator_prompt():
    client = MockClient(['最终答案：2'])
    result = assemble_step({'problem': '计算 1+1。', 'category': '数学分析', 'validated_answer': '2',
                            'validation_details': {'problem_type': 'computation'}},
                           {'configurable': {'deps': Deps(client=client)}})
    assert len(client.calls) == 1
    prompt = client.calls[0]['messages'][0]['content']
    assert '题面相关核查任务' not in prompt and 'PROOF_REVIEW' not in prompt
    assert 'Python验证代码' in prompt and '2' in result['final_response']
    assert 'proof_review' not in result


def test_batch_a_problem_topics_route_without_reference_answers():
    root = Path(__file__).resolve().parents[1]
    rows = [json.loads(line) for line in (root / 'benchmarks/proof_gap12.jsonl').read_text(encoding='utf-8').splitlines()]
    expected = {6: {'measurability', 'tails_and_truncation', 'counterexample'},
                7: {'measurability', 'counterexample'},
                8: {'inner_product', 'counterexample'}, 11: {'boundaries', 'counterexample'}}
    for row in rows:
        if row['idx'] not in expected:
            continue
        inputs = {'idx': row['idx'], 'problem': row['problem']}
        assert set(inputs) == {'idx', 'problem'}
        plan = targeted_proof_checks(inputs['problem'])
        assert expected[row['idx']] <= {task['id'] for task in plan['tasks']}
