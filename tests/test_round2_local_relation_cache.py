import hashlib
import json

import pytest

from benchmarks.check_local_relations import check


def inputs(tmp_path, extra=None):
    row = {'case_id': 'synthetic:1', 'problem': '实轴上的局部关系。',
           'final_response': '计算 $[2,3]=[2,4]$。'}
    row.update(extra or {})
    path = tmp_path/'input.jsonl'
    path.write_text(json.dumps(row, ensure_ascii=False)+'\n', encoding='utf-8')
    return path, row


def test_cache_without_domain_keeps_unknown_and_original_positions(tmp_path):
    path, row = inputs(tmp_path)
    result = check(path, tmp_path/'checked')
    assert result['statuses'] == {'unknown': 1}
    assert result['http_attempts'] == result['model_calls'] == 0
    data = json.loads((tmp_path/'checked'/'case_synthetic_1.json').read_text(encoding='utf-8'))
    record, = data['inspection']['records']
    assert row['final_response'][record['start']:record['end']] == record['raw']
    assert data['inspection']['mathematical_correctness_verified'] is False


def scopes(tmp_path, row, change=None):
    declaration = {'case_id': row['case_id'],
        'source_sha256': hashlib.sha256(row['final_response'].encode()).hexdigest(),
        'span': [0, len(row['final_response'])], 'interval_domain': 'real',
        'evidence': {'source': 'problem', 'start': 0, 'end': 2, 'quote': '实轴'},
        'interpretation': 'Explicit rational real intervals in the declared ambient domain.'}
    declaration.update(change or {})
    path = tmp_path/'scopes.json'
    path.write_text(json.dumps([declaration], ensure_ascii=False), encoding='utf-8')
    return path


def test_cache_scope_provenance_enables_only_local_computation(tmp_path):
    path, row = inputs(tmp_path)
    scope_path = scopes(tmp_path, row)
    report = check(path, tmp_path/'checked', scope_path)
    assert report['statuses'] == {'contradicts': 1}
    assert report['mathematical_correctness_verified'] is False


@pytest.mark.parametrize('extra', [
    {'truth': True}, {'reference_answer': 'no'}, {'manual_review': 'wrong'},
    {'case_id': '../../evil'}, {'final_response': ''},
])
def test_invalid_or_labelled_input_refused_before_writes(tmp_path, extra):
    path, _ = inputs(tmp_path, extra)
    with pytest.raises(ValueError):
        check(path, tmp_path/'checked')
    assert not (tmp_path/'checked').exists()


@pytest.mark.parametrize('change', [
    {'source_sha256': 'incorrect'}, {'truth': False},
    {'case_id': 'not_found'}, {'span': [-1, 5]}, {'interval_domain': 'complex'},
    {'evidence': {'source': 'human_review', 'start': 0, 'end': 2, 'quote': '实轴'}},
    {'evidence': {'source': 'problem', 'start': 0, 'end': 2, 'quote': '假的'}},
])
def test_invalid_scopes_refused_before_writes(tmp_path, change):
    path, row = inputs(tmp_path)
    scope_path = scopes(tmp_path, row, change)
    with pytest.raises(ValueError):
        check(path, tmp_path/'checked', scope_path)
    assert not (tmp_path/'checked').exists()


def test_no_silent_overwrite_or_mixed_reuse(tmp_path):
    path, _ = inputs(tmp_path)
    check(path, tmp_path/'checked')
    original = (tmp_path/'checked'/'report.json').read_bytes()
    with pytest.raises(ValueError):
        check(path, tmp_path/'checked')
    assert (tmp_path/'checked'/'report.json').read_bytes() == original


def test_duplicate_ids_refused(tmp_path):
    path, row = inputs(tmp_path)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row)+'\n')
    with pytest.raises(ValueError):
        check(path, tmp_path/'checked')


@pytest.mark.parametrize('case_id', ['synthetic_1', 'SYNTHETIC:1'])
def test_artifact_name_collisions_refused_before_writes(tmp_path, case_id):
    path, row = inputs(tmp_path)
    row['case_id'] = case_id
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row)+'\n')
    with pytest.raises(ValueError):
        check(path, tmp_path/'checked')
    assert not (tmp_path/'checked').exists()
