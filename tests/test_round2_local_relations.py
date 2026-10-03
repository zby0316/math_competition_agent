"""Exact local computations, independent boundary oracle and fail-closed inputs."""
from fractions import Fraction
from itertools import product

import pytest

from benchmarks.local_relations import LIMITS, inspect_document, verify_relation


@pytest.mark.parametrize('expression,status', [
    ('1/3+1/6=1/2', 'matches'), ('0.1+0.2=0.3', 'matches'),
    ('-(-3/7)=3/7', 'matches'), ('(2-5)*3=-9', 'matches'),
    ('2<3<=3<4', 'matches'), ('2<=3=4', 'contradicts'),
    ('2<2', 'contradicts'), ('2!=3', 'matches'), ('3>=-1', 'matches'),
    ('1/7>1/6', 'contradicts'), ('{1/3,2}={2,1/3,2}', 'matches'),
    ('{}=∅', 'matches'), ('{1,2}⊆{1,2}', 'matches'),
    ('{1}⊊{1,2}', 'matches'), ('{1,2}⊊{1,2}', 'contradicts'),
    ('{1,2}⊋{1}', 'matches'), ('{1,2}⊇{1,2}', 'matches'),
    ('{1,2}∩{2,3}={2}', 'matches'),
    ('{1,2}∪{2,3}={1,2,3}', 'matches'),
    (r'{1,2}\{2,3}={1}', 'matches'),
    ('[1/3,5/3]∩(1/3,2)=(1/3,5/3]', 'matches'),
    ('[1/3,5/3]∩(1/3,2)=[1/3,5/3]', 'contradicts'),
    ('(2,2]=∅', 'matches'), ('[2,2]={2}', 'matches'),
    ('[3,1]=∅', 'matches'),
    ('[-2,2]∩[-1,3]=[-1,2]', 'matches'),
    ('([1,3]∪[5,7])∩[2,6]=[2,3]∪[5,6]', 'matches'),
    (r'([-3,3]\[-1,1])∩[-2,2]=[-2,-1)∪(1,2]', 'matches'),
])
def test_exact_relations(expression, status):
    result = verify_relation(expression, interval_domain='real')
    assert result['status'] == status, result
    assert result['mathematical_correctness_verified'] is False


@pytest.mark.parametrize('expression,status', [
    (r'\frac{2}{7}+\frac{3}{7}=\frac{5}{7}', 'matches'),
    (r'\left[\frac{-1}{2},2\right]\cap(0,3)=(0,2]', 'matches'),
    (r'\{1,\frac{2}{3}\}\setminus\{1\}=\{\frac{2}{3}\}', 'matches'),
    (r'\{\}=\varnothing', 'matches'),
    (r'{1}=1', 'matches'),  # TeX invisible numeric grouping, not a set.
    (r'\{1\}=1', 'unknown'),
    (r'\{1\}\subseteq\{1,2\}', 'matches'),
    (r'\{1\}\subsetneq\{1\}', 'contradicts'),
    (r'\{1\}\subset\{1,2\}', 'unknown'),
])
def test_latex_restricted_notation(expression, status):
    assert verify_relation(expression, notation='latex', interval_domain='real')['status'] == status


@pytest.mark.parametrize('expression', [
    'x=1', 'n>=1', 'q(A)=[1,2]=[2,3]', '1=1 trailing', '1=',
    '(1=1', '[1,2}=∅', '{1,}=∅', '1/0=2', '0/0=0',
    '[0,∞)=∅', '1e3=1000', 'True=1', '2**3=8', '2^3=8',
    '__import__("os").system("echo unsafe")=1',
    r'[1,3]\[2,4]∩[0,5]=[1,2)',
    r'[1,3]\[2,4]∪[0,5]=[0,5]', r'{1,2}\{2}\{3}={1}',
    r'\sqrt{4}=2', r'\text{false}:1=2',
    '{1}⊂{1,2}', '1⊆2', '{1}< {2}', '{}=0',
])
def test_unsupported_input_does_not_salvage_suffix(expression):
    result = verify_relation(expression, notation='latex' if expression.startswith('\\') else 'plain',
                             interval_domain='real')
    assert result['status'] == 'unknown', result
    assert 'checks' not in result


def test_domain_is_explicit_even_when_interval_degenerate():
    for expression in ['[2,3]=[2,3]', '[2,2]={2}', '[3,2]=∅']:
        result = verify_relation(expression)
        assert result['reason'] == 'real_interval_domain_not_declared'


def test_exact_rational_evidence_and_difference_witness():
    numeric = verify_relation('0.1+0.2!=0.3')
    assert numeric['checks'][0]['evidence'] == {
        'left': '3/10', 'right': '3/10', 'left_minus_right': '0'}
    interval = verify_relation('(1/3,5/3]=[1/3,5/3]', interval_domain='real')
    evidence = interval['checks'][0]['evidence']
    assert evidence['left_minus_right'] == {'type': 'empty'}
    assert evidence['right_minus_left_witness'] == {
        'point': '1/3', 'in_left': False, 'in_right': True}


def test_all_chain_links_are_kept():
    result = verify_relation('1=1<2=3')
    assert result['status'] == 'contradicts'
    assert [c['holds'] for c in result['checks']] == [True, True, False]


@pytest.mark.parametrize('expression', [
    '1'*33+'=1', '(' * 17 + '1' + ')' * 17 + '=1',
    '1=' * 9 + '1', '1 ' * 130 + '=1',
    ' '*2049 + '1=1', '{'+','.join(str(i) for i in range(65))+'}=∅',
    '+'.join(['1']*100)+'=100',
    '*'.join(['99999999999999999999999999999999']*6)+'=1',
    '∅∪'*25+'∅=∅',
])
def test_capacity_limits_fail_closed(expression):
    assert verify_relation(expression)['status'] == 'unknown'


def test_whole_multiline_formula_and_positions():
    text = '计算如下。\n\\[\n  1/3+1/6\n  =1/2\n\\]\n完成。'
    report = inspect_document(text)
    record, = report['records']
    assert record['status'] == 'matches'
    assert record['raw'] == text[record['start']:record['end']]
    assert report['whole_document_coverage'] == 'unknown'
    assert record['assertion_role_verified'] is False


def test_tex_array_spacing_is_not_a_display_delimiter():
    text = r'$$\begin{cases}1, & x>0,\\[4pt]0,& x<0.\end{cases}$$'+'\n\n$1=1$'
    report = inspect_document(text)
    assert len(report['records']) == 2
    assert report['records'][0]['status'] == 'unknown'
    assert report['records'][1]['status'] == 'matches'
    assert not report['unknown_ranges']


@pytest.mark.parametrize('text', [
    '如果 $1=2$，则矛盾。', '假设 $1=2$。', '$1=2$ 不成立。',
    '请核查 $1=2$ 是否成立？', '引用：“$1=2$”。',
    'Suppose $1=2$.', 'This is not $1=2$.',
])
def test_hypothesis_negation_and_quote_context_unknown(text):
    record, = inspect_document(text)['records']
    assert record['status'] == 'unknown'
    assert record['reason'] == 'assertion_context_unknown'


@pytest.mark.parametrize('text', [
    '````python\n$1=2$\n````\n', '~~~\n$1=2$\n~~~\n',
    '```\n$1=2$', '<!-- $1=2$ -->', '> $1=2$\n', '`$1=2$`',
])
def test_code_comment_blockquote_are_excluded(text):
    assert inspect_document(text)['records'] == []


@pytest.mark.parametrize('text', ['$$1=2$', '$1=2$$', '\\[1=2', '$$$1=2$$$', '$1=2'])
def test_unbalanced_delimiters_are_not_recovered_as_valid_subspan(text):
    report = inspect_document(text)
    assert report['records'] == []
    assert report['unknown_ranges'][0]['reason'] == 'unbalanced_or_ambiguous_math_delimiters'


def test_explicit_scopes_do_not_bypass_context_guards():
    text = '若 $[2,3]=[2,4]$，推出矛盾。'
    report = inspect_document(text, scopes=[{'span': [0, len(text)], 'interval_domain': 'real'}])
    assert report['records'][0]['status'] == 'unknown'


def test_explicit_scope_and_list_boundaries():
    text = '1. $[2,3]=[2,4]$。\n2. $1=2$ 不是正确等式。'
    scopes = [{'span': [0, len(text)], 'interval_domain': 'real'}]
    assert inspect_document(text)['records'][0]['reason'] == 'real_interval_domain_not_declared'
    records = inspect_document(text, scopes=scopes)['records']
    assert [r['status'] for r in records] == ['contradicts', 'unknown']


@pytest.mark.parametrize('scope', [
    {'span': [-1, 5], 'interval_domain': 'real'},
    {'span': [0, 500], 'interval_domain': 'real'},
    {'span': [False, 5], 'interval_domain': 'real'},
    {'span': [0, 5], 'interval_domain': 'complex'},
])
def test_invalid_scope_is_rejected(scope):
    with pytest.raises(ValueError):
        inspect_document('$1=1$', scopes=[scope])


def test_document_limits_bound_artifact_size():
    report = inspect_document('$1=1$\n\n' * 400)
    assert len(report['records']) <= LIMITS['document_records']
    assert report['unknown_ranges'][-1]['reason'] == 'record_capacity'
    assert inspect_document('x'*100001)['unknown_ranges'][0]['reason'] == 'document_capacity'


def test_exhaustive_interval_boundary_partition_oracle():
    """An exact partition oracle, not random sampling: membership is constant
    between every rational endpoint; check every endpoint and one midpoint per
    cell. Covers degenerate intervals, both endpoint flags and differences.
    """
    endpoints = [Fraction(-1), Fraction(0), Fraction(1)]
    intervals = [(a, b, lo, ro) for a, b in product(endpoints, repeat=2)
                 for lo, ro in product([False, True], repeat=2) if a <= b]

    def written(spec):
        a, b, lo, ro = spec
        return ('(' if lo else '[')+str(a)+','+str(b)+(')' if ro else ']')

    def contains(spec, point):
        a, b, lo, ro = spec
        return (point > a if lo else point >= a) and (point < b if ro else point <= b)

    probes = endpoints + [Fraction(-1, 2), Fraction(1, 2), Fraction(-2), Fraction(2)]
    for left, right in product(intervals, repeat=2):
        memberships = [(contains(left, p), contains(right, p)) for p in probes]
        equal = all(a == b for a, b in memberships)
        subset = all(not a or b for a, b in memberships)
        for operation, truth in [('=', equal), ('⊆', subset), ('⊊', subset and not equal)]:
            result = verify_relation(written(left)+operation+written(right), interval_domain='real')
            assert result['status'] == ('matches' if truth else 'contradicts'), result
            for evidence in result['checks'][0]['evidence'].values():
                if isinstance(evidence, dict) and 'point' in evidence:
                    p = Fraction(evidence['point'])
                    assert evidence['in_left'] == contains(left, p)
                    assert evidence['in_right'] == contains(right, p)


def test_exhaustive_set_operations_against_boundary_partition_oracle():
    endpoints = [Fraction(-1), Fraction(0), Fraction(1)]
    intervals = [(a, b, lo, ro) for a, b in product(endpoints, repeat=2)
                 for lo, ro in product([False, True], repeat=2) if a <= b]
    probes = endpoints + [Fraction(-1, 2), Fraction(1, 2), Fraction(-2), Fraction(2)]

    def written(spec):
        a, b, lo, ro = spec
        return ('(' if lo else '[')+str(a)+','+str(b)+(')' if ro else ']')

    def contains(spec, p):
        a, b, lo, ro = spec
        return (p > a if lo else p >= a) and (p < b if ro else p <= b)

    def described_contains(description, p):
        kind = description['type']
        if kind == 'empty':
            return False
        if kind == 'finite':
            return p in [Fraction(v) for v in description['points']]
        if kind == 'union':
            return any(described_contains(v, p) for v in description['components'])
        assert kind == 'real_interval'
        return contains((Fraction(description['left']), Fraction(description['right']),
                         description['left_open'], description['right_open']), p)

    for left, right in product(intervals, repeat=2):
        for symbol, operation in [('∩', lambda a, b: a and b),
                                  ('∪', lambda a, b: a or b),
                                  ('∖', lambda a, b: a and not b)]:
            membership = [operation(contains(left, p), contains(right, p)) for p in probes]
            result = verify_relation('('+written(left)+symbol+written(right)+')=∅', interval_domain='real')
            assert result['status'] == ('matches' if not any(membership) else 'contradicts')
            evidence = result['checks'][0]['evidence']
            assert [described_contains(evidence['left'], p) for p in probes] == membership
            if any(membership):
                p = Fraction(evidence['left_minus_right_witness']['point'])
                assert operation(contains(left, p), contains(right, p))


def test_finite_points_split_interval_with_exact_open_boundaries():
    result = verify_relation(r'[-2,2]\{-1,0,1}=[-2,-1)∪(-1,0)∪(0,1)∪(1,2]',
                             interval_domain='real')
    assert result['status'] == 'matches'
