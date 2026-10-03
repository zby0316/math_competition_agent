"""Offline replay of recorded proof coordination; never regenerates mathematics."""
import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import AGENT_VERSION, CONFIG
from nodes.assemble_step import assemble_step
from utils.deps import Deps, MockClient
from utils.llm_retry_guard import ChatResult
from utils.run_manifest import atomic_json, sha256


HANDOFF = '完整可用证明及状态（不可信数学候选）：\n'


def replay(run_dir, inputs, out_dir):
    items = []
    for row in inputs:
        idx = row['idx']
        calls_path = run_dir / 'calls' / f'{idx}.calls.jsonl'
        calls = [json.loads(line) for line in calls_path.read_text(encoding='utf-8').splitlines()]
        recorded = [call for call in calls if HANDOFF in call['messages'][0]['content']][-1]
        old_prompt = recorded['messages'][0]['content']
        handoff = json.loads(old_prompt.split(HANDOFF)[1].split('\n\n最后用')[0])
        category = old_prompt.split('\n类别：')[1].split('\n')[0]
        validated = old_prompt.split('现有结论（尚待证明核查）：')[1].split('\n' + HANDOFF)[0]
        rr = {key: handoff[key] for key in ['proof_body', 'coverage', 'finish_reason', 'missing_sections']}
        rr.update(answer=validated, proof_attempts=handoff['attempts'])
        client = MockClient([ChatResult(recorded['response'], recorded.get('finish_reason'))])
        client._tls = SimpleNamespace(finish_reason=recorded.get('finish_reason'),
                                      usage=recorded.get('usage'), http_attempts=0)
        result = assemble_step({'problem': row['problem'], 'category': category,
                                'reasoning_result': rr, 'validated_answer': validated,
                                'reasoning_raw_response': handoff['visible_response'],
                                'validation_details': {'problem_type': 'proof'}},
                               {'configurable': {'deps': Deps(client=client)}})
        assert len(client.calls) == 1, 'Replay must use one simulated existing coordination'
        new_prompt = client.calls[0]['messages'][0]['content']
        new_handoff = json.loads(new_prompt.split(HANDOFF)[1].split('\n\n最后用')[0])
        assert {key: new_handoff[key] for key in handoff} == handoff, 'Legacy full proof handoff changed'
        index = result['proof_review']['candidate_index']
        assert [c[0] for c in new_handoff['candidate_index']['claims']] == [c['id'] for c in index['claims']]
        sources = {}
        for source in index['sources']:
            text = new_handoff
            for part in source['path'].split('/'):
                text = text[int(part)] if isinstance(text, list) else text[part]
            assert sha256(text.encode()) == source['sha256']
            sources[source['id']] = (text, source)
        blocks = {block['id']: block for block in index['blocks']}
        for claim in index['claims']:
            block = blocks[claim['block_id']]
            text, source = sources[block['source_id']]
            assert source['formal_start'] <= block['start'] <= claim['start'] < claim['end'] <= block['end'] <= source['formal_end']
            assert text[claim['start']:claim['end']].strip()
        saved_output = json.loads((run_dir / f'{idx}.json').read_text(encoding='utf-8'))
        assert result['final_response'] == saved_output['final_response'], 'Cached public answer changed'
        old_review = next(t for t in saved_output['trace'] if t['step'] == 'proof_review')
        checks = result['proof_review']['requested_checks']
        old_ids = [task['id'] for task in old_review['requested_checks']['tasks']]
        new_ids = [task['id'] for task in checks['tasks']]
        assert set(old_ids) <= set(new_ids), 'A problem-selected task was lost'
        atomic_json(out_dir / 'prompts' / f'{idx}.json', {
            'idx': idx, 'input': row, 'messages': client.calls[0]['messages'],
            'requested_checks': checks, 'cached_call_id': recorded['call_id'],
            'cached_response_sha256': sha256(recorded['response'].encode()),
            'response_source': 'unchanged historical response, not a new generation'})
        items.append({'idx': idx, 'previous_tasks': old_ids, 'current_tasks': new_ids,
                      'added_tasks': [task for task in new_ids if task not in old_ids],
                      'sources': {task['id']: task['sources'] for task in checks['tasks']},
                      'full_handoff_equal': True, 'proof_body_characters': len(handoff['proof_body']),
                      'indexed_sources': len(index['sources']), 'indexed_blocks': len(index['blocks']),
                      'recognized_claims': len(index['claims']), 'unknown_ranges': len(index['unknowns']),
                      'index_characters': len(json.dumps(new_handoff['candidate_index'], ensure_ascii=False, separators=(',', ':'))),
                      'old_prompt_characters': len(old_prompt), 'new_prompt_characters': len(new_prompt),
                      'prompt_character_delta': len(new_prompt) - len(old_prompt),
                      'coverage': result['proof_review']['claim_coverage'],
                      'cached_final_unchanged': True, 'simulated_calls': 1, 'http_attempts': 0,
                      'mathematical_correctness_verified': False})
    report = {'status': 'offline_replay_complete', 'agent_version': AGENT_VERSION,
              'source_run': str(run_dir.resolve()), 'config': CONFIG, 'items': items,
              'http_attempts': 0,
              'limitation': 'Only selection and handoff are verified; cached errors remain historical errors.'}
    atomic_json(out_dir / 'replay.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    inputs = [json.loads(line) for line in args.input.read_text(encoding='utf-8').splitlines()]
    if any(set(row) != {'idx', 'problem'} for row in inputs):
        raise ValueError('Replay inputs must contain only idx and problem')
    report = replay(args.source_run, inputs, args.out)
    print(json.dumps({'status': report['status'], 'http_attempts': 0,
                     'items': [{k: v for k, v in item.items() if k not in {'coverage', 'sources'}}
                               for item in report['items']]}, ensure_ascii=False))
