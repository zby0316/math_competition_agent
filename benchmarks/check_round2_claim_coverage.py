"""Check complete cached public answers and compact location tables without model calls."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.proof_claims import compact_index, index_sources
from utils.proof_review import targeted_proof_checks
from utils.run_manifest import atomic_json, sha256


def check(inputs, out):
    items = []
    for row in inputs:
        text = row['final_response']
        tasks = targeted_proof_checks(row['problem'], candidate_proof=text)['tasks']
        index = index_sources([{'path': 'final_response', 'text': text, 'parsed_formal': True,
                                'finish_reason': 'stop'}], [task['id'] for task in tasks])
        blocks = {block['id']: block for block in index['blocks']}
        sections = Counter()
        for claim in index['claims']:
            block = blocks[claim['block_id']]
            assert 0 <= block['start'] <= claim['start'] < claim['end'] <= block['end'] <= len(text)
            assert text[claim['start']:claim['end']].strip()
            sections[block['section']] += 1
        compact = compact_index(index)
        assert [c[0] for c in compact['claims']] == [c['id'] for c in index['claims']]
        atomic_json(out / f"{row['idx']}.json", {'idx': row['idx'], 'source': 'unchanged cached public answer',
            'final_response_sha256': sha256(text.encode()), 'index': index, 'prompt_index': compact})
        items.append({'idx': row['idx'], 'answer_characters': len(text),
            'recognized_claims': len(index['claims']), 'claims_by_section': dict(sections),
            'unknown_ranges': len(index['unknowns']), 'original_offsets_checked': True,
            'final_response_sha256': sha256(text.encode()), 'mathematical_correctness_verified': False})
    report = {'status': 'complete_cache_location_check', 'http_attempts': 0, 'simulated_model_calls': 0,
              'items': items, 'limitation': 'Locations only; no detection benefit or mathematical certification.'}
    atomic_json(out / 'report.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.input.read_text(encoding='utf-8').splitlines()]
    if any(set(row) != {'idx', 'problem', 'final_response'} for row in rows):
        raise ValueError('Cache must contain only idx, original problem and public answer, without labels')
    print(json.dumps(check(rows, args.out), ensure_ascii=False))
