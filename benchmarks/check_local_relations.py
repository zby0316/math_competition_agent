"""Label-free, standalone cached-answer checks. No solver or API imports."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.local_relations import VERSION, inspect_document


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def validated_scopes(rows, declarations):
    """Validate positions/provenance; a declaration is not a proved hypothesis."""
    cases = {row['case_id']: row for row in rows}
    result = {key: [] for key in cases}
    for declaration in declarations:
        required = {'case_id', 'source_sha256', 'span', 'interval_domain', 'evidence', 'interpretation'}
        if not isinstance(declaration, dict) or set(declaration) != required:
            raise ValueError('Scope schema must contain notation provenance only')
        case_id = declaration['case_id']
        if case_id not in cases:
            raise ValueError('Unknown scope case')
        row = cases[case_id]
        if declaration['source_sha256'] != digest(row['final_response'].encode()):
            raise ValueError('Scope source hash mismatch')
        evidence = declaration['evidence']
        if not isinstance(evidence, dict) or set(evidence) != {'source', 'start', 'end', 'quote'}:
            raise ValueError('Invalid notation evidence schema')
        if evidence['source'] not in {'problem', 'final_response'}:
            raise ValueError('Notation evidence must come from original inputs')
        text = row[evidence['source']]
        a, b = evidence['start'], evidence['end']
        if (type(a) is not int or type(b) is not int or not 0 <= a < b <= len(text)
                or evidence['quote'] != text[a:b]):
            raise ValueError('Notation evidence positions mismatch')
        if not isinstance(declaration['interpretation'], str) or not declaration['interpretation'].strip():
            raise ValueError('Missing notation interpretation')
        result[case_id].append(declaration)
    return result


def check(input_path, output_dir, scopes_path=None):
    raw_input = input_path.read_bytes()
    rows = [json.loads(line) for line in raw_input.decode('utf-8').splitlines() if line.strip()]
    expected = {'case_id', 'problem', 'final_response'}
    if (not rows or any(not isinstance(row, dict) or set(row) != expected
                        or any(not isinstance(row[key], str) or not row[key] for key in expected)
                        for row in rows)):
        raise ValueError('Input must contain only case_id, original problem and complete public answer')
    if len({row['case_id'] for row in rows}) != len(rows):
        raise ValueError('Duplicate case_id')
    if any(not row['case_id'].replace('_', '').replace('-', '').replace(':', '').isalnum() for row in rows):
        raise ValueError('Unsafe case_id')
    names = ['case_'+row['case_id'].replace(':', '_')+'.json' for row in rows]
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError('Case ids produce colliding artifact names')
    declarations = json.loads(scopes_path.read_text(encoding='utf-8')) if scopes_path else []
    if not isinstance(declarations, list):
        raise ValueError('Scopes must be a list')
    per_case = validated_scopes(rows, declarations)
    # All validation happens before artifact writes. Output must be new; do not
    # overwrite frozen source caches or silently mix different runs.
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError('Output directory must be empty; use an independent directory')
    if input_path.resolve().is_relative_to(output_dir.resolve()):
        raise ValueError('Output directory contains source input')
    reports, summaries = [], []
    statuses, reasons = Counter(), Counter()
    for row, artifact_name in zip(rows, names):
        checked = inspect_document(row['final_response'], scopes=per_case[row['case_id']])
        for record in checked['records']:
            assert row['final_response'][record['start']:record['end']] == record['raw']
            statuses[record['status']] += 1
            if record['status'] == 'unknown':
                reasons[record['reason']] += 1
        reports.append({'case_id': row['case_id'], 'artifact_name': artifact_name,
                        'problem_sha256': digest(row['problem'].encode()),
                        'domain_declarations': per_case[row['case_id']], 'inspection': checked})
        summaries.append({'case_id': row['case_id'], 'artifact_name': artifact_name,
                          'answer_characters': len(row['final_response']),
                          'answer_sha256': checked['source_sha256'],
                          'relation_spans': len(checked['records']),
                          'statuses': dict(Counter(r['status'] for r in checked['records'])),
                          'unknown_ranges': len(checked['unknown_ranges']),
                          'source_positions_checked': True})
    source_files = {name: digest((ROOT/name).read_bytes()) for name in
                    ('benchmarks/local_relations.py', 'benchmarks/check_local_relations.py')}
    report = {'version': VERSION, 'status': 'offline_cache_check_complete', 'http_attempts': 0,
              'model_calls': 0, 'input_sha256': digest(raw_input),
              'scopes_sha256': digest(scopes_path.read_bytes()) if scopes_path else None,
              'standalone_source_files': source_files, 'cases': len(rows), 'items': summaries,
              'relation_spans': sum(statuses.values()), 'statuses': dict(statuses),
              'unknown_reasons': dict(reasons), 'domain_declarations': len(declarations),
              'mathematical_correctness_verified': False, 'whole_proof_coverage': 'unknown',
              'limitation': 'Local computations only. Caller notation and assertion roles are not certified.'}
    output_dir.mkdir(parents=True, exist_ok=True)
    for item in reports:
        write_json(output_dir/item['artifact_name'], item)
    write_json(output_dir/'report.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--scopes', type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.input, args.out, args.scopes), ensure_ascii=False, indent=2))
