"""Local candidate cue coverage cannot certify mathematics or override an answer."""
import json
from types import SimpleNamespace

import pytest

from config import CONFIG
from nodes.assemble_step import assemble_step
from utils.deps import Deps, MockClient
from utils.proof_claims import (MAX_CLAIMS, claim_coverage, compact_index, index_sources, indexed_handoff)
from utils.proof_review import proof_handoff
from utils.score_budget import ScoreBudget
from utils.wall_clock import TimeBudget


def resolve(payload, path):
    value = payload
    for part in path.split('/'):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def one_index(text='a=b。'):
    return index_sources([{'path': 'proof_body', 'text': text, 'parsed_formal': True, 'finish_reason': 'stop'}])


def checks_for(index, **changes):
    return [{'claim_id': c['id'], 'status': 'checked', 'premises': '原题的实数条件',
             'basis': '逐项代入定义得到等式', **changes} for c in index['claims']]


def test_formal_full_sections_are_indexed_without_copying_text():
    text = ('## 摘要\na=b。\n## 正文\n若参数t大于阈值，则u≤v。\n'
            '## 结论\na=b。\n## 关键验证点\n若改动条件，该关系不再成立。')
    state = {'reasoning_raw_response': text, 'reasoning_result': {'proof_body': '若参数t大于阈值，则u≤v。',
             'finish_reason': 'stop', 'answer': 'a=b。'}}
    payload = indexed_handoff(state, ['premises_and_coverage'])
    old = json.loads(proof_handoff(state))
    assert all(payload[key] == value for key, value in old.items())
    index = payload['candidate_index']
    assert {b['section'] for b in index['blocks']} == {'## 摘要', '## 正文', '## 结论', '## 关键验证点'}
    assert len(index['sources']) == 1  # Parsed body and answer are already in formal text.
    encoded = json.dumps(index, ensure_ascii=False)
    assert text not in encoded and '不再成立' not in encoded
    sources = {s['id']: s for s in index['sources']}
    blocks = {b['id']: b for b in index['blocks']}
    fragments = []
    for claim in index['claims']:
        block = blocks[claim['block_id']]
        original = resolve(payload, sources[block['source_id']]['path'])
        fragments.append(original[claim['start']:claim['end']])
        assert block['start'] <= claim['start'] < claim['end'] <= block['end']
    assert fragments.count('a=b。') == 2
    assert any('不再成立' in fragment for fragment in fragments)
    assert not index['mathematical_correctness_verified']


def test_multiline_display_with_blank_lines_keeps_one_relation_span():
    text = '## 正文\n证明等式。\n\n$$\na=b\n\n=c\n$$\n\n结论成立。'
    index = one_index(text)
    formulas = [text[c['start']:c['end']] for c in index['claims'] if 'relation' in c['kinds']]
    assert formulas == ['$$\na=b\n\n=c\n$$']
    assert index == one_index(text)


def test_missing_parsed_sections_added_once_with_resolvable_paths():
    state = {'reasoning_result': {'proof_body': 'a=b。', 'summary': '每个参数满足条件。',
             'analysis': '使用连续性。', 'answer': '结论成立。', 'validation_points': ['断言失效。'],
             'finish_reason': 'stop'}}
    payload = indexed_handoff(state, ['topology'])
    assert payload['parsed_sections']['validation_points_0'] == '断言失效。'
    for source in payload['candidate_index']['sources']:
        assert len(resolve(payload, source['path'])) == source['characters']
    assert len([s for s in payload['candidate_index']['sources'] if s['path'].endswith('/analysis')]) == 1


def test_draft_prefix_is_unknown_and_exact_formal_offsets_are_preserved():
    text = 'Thinking Process:\n先试猜结果。\n## 正文\na=b。'
    index = index_sources([{'path': 'visible_response', 'text': text, 'finish_reason': 'stop'}])
    assert index['sources'][0]['formal_start'] == text.index('## 正文')
    claim = index['claims'][0]
    assert text[claim['start']:claim['end']] == 'a=b。'
    assert any(u['reason'] == 'unclassified_prefix' for u in index['unknowns'])


@pytest.mark.parametrize('text', [
    'Thinking Process:\na=b。',
    '## Thinking Process\na=b。',
    '```text\n## 正文\na=b。\n```',
    '> ## 正文\n> a=b。',
    '<!-- ## 正文\na=b。 -->',
    '## 模板样例\na=b。',
])
def test_unclassified_or_quoted_raw_content_is_not_formal_claim_evidence(text):
    index = index_sources([{'path': 'visible_response', 'text': text}])
    assert not index['claims'] and index['unknowns']


def test_format_failed_fields_stay_in_handoff_but_are_not_indexed():
    state = {'reasoning_result': {'proof_body': 'a=b。', 'answer': 'x=y。',
                                 'format_failure': 'draft_without_formal_proof'}}
    payload = indexed_handoff(state)
    assert payload['proof_body'] == 'a=b。'
    assert not payload['candidate_index']['claims']
    assert any(u['reason'] == 'format_failure' for u in payload['candidate_index']['unknowns'])


def test_duplicate_sources_do_not_hide_truncation_flags():
    sources = [{'path': 'attempts/0/visible_response', 'text': '## 正文\na=b。', 'finish_reason': 'stop'},
               {'path': 'proof_body', 'text': 'a=b。', 'parsed_formal': True, 'finish_reason': 'length'}]
    index = index_sources(sources)
    assert len(index['sources']) == len(index['claims']) == 1
    assert any(u['reason'] == 'candidate_incomplete_or_truncated' for u in index['unknowns'])


def test_capacity_limits_metadata_not_original_candidate():
    text = '\n'.join(f'a_{i}=b_{i}。' for i in range(MAX_CLAIMS + 10))
    payload = indexed_handoff({'reasoning_result': {'proof_body': text, 'finish_reason': 'stop'}})
    assert payload['proof_body'] == text
    index = payload['candidate_index']
    assert len(index['claims']) == MAX_CLAIMS
    assert any(u['reason'] == 'claim_index_capacity' for u in index['unknowns'])
    coverage = claim_coverage({'status': 'none_reported', 'checks': checks_for(index)}, index, 'stop')
    assert coverage['status'] == 'uncertain' and not coverage['mathematical_correctness_verified']


def test_unknown_prose_is_aggregated_instead_of_expanding_every_sentence():
    index = one_index('需要解释。' * 2000)
    assert not index['claims'] and len(index['unknowns']) == 1
    assert index['unknowns'][0]['segments'] == 2000


def test_all_located_records_can_be_present_but_math_is_not_verified():
    index = one_index()
    review = {'status': 'none_reported', 'checks': checks_for(index, basis='0=1，所以原式成立')}
    coverage = claim_coverage(review, index, 'stop')
    assert coverage['status'] == 'records_present_unverified'
    assert coverage['referenced_with_fields_count'] == 1
    assert not coverage['mathematical_correctness_verified']


@pytest.mark.parametrize('changes,flag', [
    ({'claim_id': 'C999'}, 'unknown_or_missing_claim_id'),
    ({'claim_id': []}, 'unknown_or_missing_claim_id'),
    ({'span': [0, 999]}, 'invalid_span'),
    ({'span': [False, 4]}, 'invalid_span'),
    ({'basis': ''}, 'missing_concrete_basis'),
    ({'basis': '已核查'}, 'missing_concrete_basis'),
    ({'premises': '同上'}, 'missing_concrete_premises'),
    ({'status': 'unresolved'}, 'model_unresolved'),
    ({'status': []}, 'invalid_status'),
])
def test_bad_reference_scope_or_evidence_remains_uncertain(changes, flag):
    index = one_index()
    coverage = claim_coverage({'status': 'none_reported', 'checks': checks_for(index, **changes)}, index, 'stop')
    assert coverage['status'] == 'uncertain'
    assert flag in coverage['records'][0]['flags']
    assert coverage['missing_or_uncertain_claim_ids'] == ['C001']


def test_partial_coverage_never_implies_unmentioned_claim_passed():
    index = one_index('a=b。\nc=d。')
    coverage = claim_coverage({'status': 'none_reported', 'checks': checks_for(index)[:1]}, index, 'stop')
    assert coverage['referenced_with_fields_count'] == 1 and coverage['recognized_claim_count'] == 2
    assert coverage['missing_or_uncertain_claim_ids'] == ['C002']


def test_conflicting_duplicate_records_keep_claim_uncertain():
    index = one_index()
    checks = checks_for(index) + checks_for(index, status='unresolved')
    coverage = claim_coverage({'status': 'none_reported', 'checks': checks}, index, 'stop')
    assert coverage['referenced_with_fields_count'] == 0


@pytest.mark.parametrize('finish', ['length', None, 'content_filter'])
def test_unknown_or_truncated_response_does_not_certify_coverage(finish):
    index = one_index()
    coverage = claim_coverage({'status': 'none_reported', 'checks': checks_for(index)}, index, finish)
    assert coverage['status'] == 'uncertain' and coverage['flags']


def test_legacy_missing_or_malformed_records_remain_readable_and_unknown():
    for review in ({'status': 'none_reported', 'checks': [{'claim': 'a=b', 'basis': '依据'}]},
                   {'status': 'unknown', 'record_parse_error': 'invalid_json_or_shape'}):
        coverage = claim_coverage(review, one_index(), 'stop')
        assert coverage['status'] == 'uncertain'
        assert not coverage['mathematical_correctness_verified']


def test_unterminated_formula_is_preserved_and_flagged_unknown():
    index = one_index('$$\na=b')
    assert any(u['reason'] == 'unclosed_or_unparsed_math' for u in index['unknowns'])


def test_scope_and_validity_cues_associate_only_existing_methods():
    index = index_sources([{'path': 'proof_body', 'parsed_formal': True,
        'text': '若失去可测性，该断言不再成立。\n复内积等式u=v。'}], ['measurability'])
    assert index['claims'][0]['task_ids'] == ['measurability']
    assert {'condition', 'property', 'validity'} <= set(index['claims'][0]['kinds'])
    assert 'task_ids' not in index['claims'][1]  # Unselected topic is not promoted to a premise/task.


def test_existing_coordinator_one_call_and_public_proof_unchanged_by_coverage():
    public = '摘要\n结论需核对。\n正文\n必要论证保留。\n结论\n该关系未决。'
    note = {'status': 'issues_corrected', 'issues': [], 'checks': [
        {'claim_id': 'C001', 'status': 'checked', 'premises': '实数条件', 'basis': '逐项定义'}]}
    client = MockClient([public + '\n<!-- PROOF_REVIEW ' + json.dumps(note, ensure_ascii=False) + ' -->'])
    client._tls = SimpleNamespace(finish_reason='stop', http_attempts=1)
    clock, tokens = TimeBudget(), ScoreBudget()
    state = {'problem': '证明两个等式。', 'validated_answer': '旧结论。',
             'reasoning_result': {'proof_body': 'a=b。\nc=d。', 'finish_reason': 'stop'},
             'validation_details': {'problem_type': 'proof'}}
    result = assemble_step(state, {'configurable': {'deps': Deps(client=client, time_budget=clock, token_budget=tokens)}})
    assert len(client.calls) == len(clock.spend_log()) == 1
    assert client.calls[0]['max_tokens'] == CONFIG['max_tokens']['coordinator']
    assert client.calls[0]['temperature'] == CONFIG['temperatures']['coordinator']
    assert result['final_response'] == public
    assert result['proof_review']['status'] == 'unresolved'
    assert result['proof_review']['model_reported_status'] == 'issues_corrected'
    assert result['proof_review']['claim_coverage']['missing_or_uncertain_claim_ids'] == ['C002']


@pytest.mark.parametrize('protected', ['locked', 'expired'])
def test_protected_candidates_do_not_build_index_or_make_extra_calls(monkeypatch, protected):
    def forbidden(*args):
        raise AssertionError('Protected candidate must not be reindexed or regenerated')
    monkeypatch.setattr('utils.proof_claims.indexed_handoff', forbidden)
    now = [0]
    clock = TimeBudget(clock=lambda: now[0])
    state = {'problem': '证明命题。', 'validated_answer': '结论成立。',
             'reasoning_result': {'proof_body': '必要证明。'}, 'validation_details': {'problem_type': 'proof'}}
    if protected == 'locked':
        state.update(answer_locked=True, arbiter_decision='reasoning')
    else:
        now[0] = 1201
    client = MockClient([])
    result = assemble_step(state, {'configurable': {'deps': Deps(client=client, time_budget=clock)}})
    assert not client.calls and '结论成立' in result['final_response']


def test_parsed_section_inside_raw_draft_has_original_offsets_without_new_text_copy():
    raw = 'Thinking Process:\n暂存a=b。\n其他推理。'
    state = {'reasoning_raw_response': raw,
             'reasoning_result': {'proof_body': 'c=d。', 'answer': 'a=b。', 'finish_reason': 'stop',
                 'proof_attempts': [{'visible_response': raw, 'finish_reason': 'stop'}]}}
    handoff = indexed_handoff(state)
    assert 'parsed_sections' not in handoff
    sources = {s['id']: s for s in handoff['candidate_index']['sources']}
    blocks = {b['id']: b for b in handoff['candidate_index']['blocks']}
    claim = next(c for c in handoff['candidate_index']['claims']
                 if sources[blocks[c['block_id']]['source_id']]['path'] == 'attempts/0/visible_response')
    assert raw[claim['start']:claim['end']] == 'a=b。'
    assert handoff['candidate_index']['unknowns']  # Remaining raw draft still unknown.


def test_legacy_attempt_reference_is_not_misreported_as_unparsed_proof():
    formal = '## 详细解题步骤\na=b。\n## 最终答案\na=b。'
    handoff = indexed_handoff({'reasoning_raw_response': formal,
        'reasoning_result': {'proof_body': 'a=b。', 'finish_reason': 'stop',
            'proof_attempts': [{'visible_response': formal, 'finish_reason': 'stop'}]}})
    assert handoff['visible_response'] == '见 attempts 中的完整响应'
    assert not any(u.get('path') == 'visible_response' for u in handoff['candidate_index']['unknowns'])


def test_truncated_history_cannot_exhaust_index_before_selected_parsed_proof():
    partial = '## 详细解题步骤\n' + '\n'.join('x=y。' for _ in range(MAX_CLAIMS + 5))
    payload = indexed_handoff({'reasoning_result': {'proof_body': 'a=b。', 'answer': 'c=d。', 'finish_reason': 'length',
        'proof_attempts': [{'visible_response': partial, 'finish_reason': 'length'}]}})
    assert payload['candidate_index']['sources'][0]['path'] == 'proof_body'
    assert payload['candidate_index']['claims'][0]['start'] == 0
    assert payload['candidate_index']['sources'][1]['path'] == 'parsed_sections/answer'
    assert payload['proof_body'] == 'a=b。'
    assert any(u['reason'] == 'claim_index_capacity' for u in payload['candidate_index']['unknowns'])


def test_compact_tables_preserve_every_location_kind_task_and_unknown():
    index = index_sources([{'path': 'proof_body', 'text': '若可测性失效，a=b不成立。\n' * 20 + '解释。',
                            'parsed_formal': True}], ['measurability', 'counterexample'])
    compact = compact_index(index)
    assert len(compact['unknowns']) == len(index['unknowns'])
    for row, claim in zip(compact['claims'], index['claims']):
        assert row[:4] == [claim[k] for k in ('id', 'block_id', 'start', 'end')]
        assert [compact['kinds'][n] for n in row[4]] == claim['kinds']
        assert [compact['tasks'][n] for n in row[5]] == claim.get('task_ids', [])
    assert len(json.dumps(compact)) < len(json.dumps(index))


def test_plain_english_proof_discussion_is_not_a_formal_heading():
    index = index_sources([{'path': 'visible_response', 'text': 'Proof should use a=b in the next attempt.'}])
    assert not index['claims']
