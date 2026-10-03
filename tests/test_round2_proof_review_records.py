"""Preserve failed model metadata without repairing JSON or certifying math."""
import json

import pytest

from utils.proof_review import extract_review


@pytest.mark.parametrize('payload,complete,expected_error', [
    (r'{"status":"unresolved","checks":[{"basis":"\langle x,y\rangle"}]}', True, 'invalid_json_or_shape'),
    ('["模型记录"]', True, 'invalid_json_or_shape'),
    ('{"status":', False, 'unterminated_record'),
])
def test_invalid_record_keeps_evidence_and_public_proof(payload, complete, expected_error):
    proof = r'由 $\langle e_n,y\rangle=\overline{y_n}$ 得所需结论。'
    raw = proof + '\n<!-- PROOF_REVIEW ' + payload + (' -->' if complete else '')
    visible, review = extract_review(raw)
    assert visible == proof
    assert review['status'] == 'unknown'
    assert review['record_parse_error'] == expected_error
    assert review['raw_record'].strip() == payload
    assert review['mathematical_correctness_verified'] is False
    assert 'checks' not in review


def test_multiline_valid_record_preserves_basis_without_rewriting_latex():
    basis = r'两边均为 $\langle x,y\rangle$；仍缺少所需的非零虚部代入。'
    note = {'status': 'unresolved', 'issues': [], 'checks': [
        {'task_id': 'inner_product', 'status': 'unresolved', 'basis': basis}]}
    visible, review = extract_review('完整正文\n<!-- PROOF_REVIEW\n' + json.dumps(note,ensure_ascii=False,indent=2) + '\n-->')
    assert visible == '完整正文'
    assert review['status'] == 'unresolved' and review['checks'] == note['checks']
    assert review['checks'][0]['basis'] == basis
    assert 'record_parse_error' not in review
    assert review['mathematical_correctness_verified'] is False
