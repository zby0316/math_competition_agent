"""Standalone, bounded exact relation experiment; never imported by the solver.

No eval, sympify or general LaTeX parser. Results concern the supplied local
objects, not the truth of a surrounding proof, assumption or quoted statement.
"""
from fractions import Fraction
import hashlib
import re

from sympy import FiniteSet, Interval, Rational, S, Union

VERSION = 1
LIMITS = {'characters': 2048, 'tokens': 256, 'depth': 16, 'nodes': 128,
          'digits': 32, 'rational_bits': 512, 'set_atoms': 24,
          'finite_points': 64, 'relations': 8, 'document_characters': 100000,
          'document_records': 256}
RELATIONS = {'=', '!=', '<', '<=', '>', '>=', 'subseteq', 'supseteq',
             'subsetneq', 'supsetneq', 'ambiguous_subset', 'ambiguous_supset'}
COMMANDS = {
    'frac': 'frac', 'dfrac': 'frac', 'tfrac': 'frac', 'cdot': '*', 'times': '*',
    'cap': 'intersect', 'cup': 'union', 'setminus': 'difference',
    'emptyset': 'empty', 'varnothing': 'empty', 'le': '<=', 'leq': '<=',
    'ge': '>=', 'geq': '>=', 'ne': '!=', 'neq': '!=',
    'subseteq': 'subseteq', 'supseteq': 'supseteq',
    'subsetneq': 'subsetneq', 'subsetneqq': 'subsetneq',
    'supsetneq': 'supsetneq', 'supsetneqq': 'supsetneq',
    'subset': 'ambiguous_subset', 'supset': 'ambiguous_supset',
}
UNICODE = {'∩': 'intersect', '∪': 'union', '∖': 'difference', '∅': 'empty',
           '≤': '<=', '≥': '>=', '≠': '!=', '⊆': 'subseteq', '⊇': 'supseteq',
           '⊊': 'subsetneq', '⊋': 'supsetneq', '⊂': 'ambiguous_subset',
           '⊃': 'ambiguous_supset', '−': '-'}
OPEN = {'(', '[', '{', 'set{'}
CLOSE = {')', ']', '}', 'set}'}
NUMBER = re.compile(r'(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)')
RELATION_CUE = re.compile(r'[=<>≤≥≠⊆⊇⊊⊋⊂⊃]|\\(?:leq?|geq?|neq?|subset\w*|supset\w*)\b')
DELIMITER = re.compile(r'(?<!\\)(?:\$+|\\[\[\]()])')
EXCLUDED = re.compile(r'(?ms)^ {0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n.*?'
                      r'(?:^ {0,3}(?P=fence)[^\n]*(?:\n|$)|\Z)|'
                      r'<!--.*?(?:-->|\Z)|^[ \t]*>[^\n]*|(?P<ticks>`+)[^`]*?(?P=ticks)')
CONTEXT = re.compile(r'若|如果|假设|假定|设(?:\s|\$)|令|取(?:\s|\$)|当.{0,50}时|'
                     r'不成立|不满足|并非|不是|否定|反证|错误|误写|声称|引用|是否|例如|示例|'
                     r'(?i:\bif\b|\bunless\b|\bassum\w*\b|\bsuppose\b|\bnot\b|\bfalse\b|\bexample\b|\bquot\w*\b)|[?？“”]')


class Unknown(ValueError):
    pass


def tokenize(text, notation):
    if notation not in {'plain', 'latex'}:
        raise Unknown('unknown_notation')
    if len(text) > LIMITS['characters']:
        raise Unknown('character_capacity')
    tokens, pos = [], 0
    while pos < len(text):
        char = text[pos]
        if char.isspace():
            pos += 1
            continue
        number = NUMBER.match(text, pos)
        if number:
            value = number.group()
            if sum(c.isdigit() for c in value) > LIMITS['digits']:
                raise Unknown('digit_capacity')
            tokens.append(value)
            pos = number.end()
        elif char == '\\':
            if notation == 'plain':
                tokens.append('difference')
                pos += 1
            elif pos + 1 < len(text) and text[pos + 1] in '{}':
                tokens.append('set' + text[pos + 1])
                pos += 2
            elif pos + 1 < len(text) and text[pos + 1] in ',;! ':
                pos += 2  # TeX spacing only.
            else:
                command = re.match(r'\\([A-Za-z]+)', text[pos:])
                if command is None:
                    raise Unknown('unsupported_command')
                name = command[1]
                pos += len(command[0])
                if name in {'left', 'right', 'quad', 'qquad'}:
                    continue
                if name not in COMMANDS:
                    raise Unknown('unsupported_command:' + name)
                tokens.append(COMMANDS[name])
        elif text[pos:pos + 2] in {'<=', '>=', '!='}:
            tokens.append(text[pos:pos + 2])
            pos += 2
        elif char in UNICODE:
            tokens.append(UNICODE[char])
            pos += 1
        elif char in '()+-*/[],{}=<>':
            tokens.append('set' + char if char in '{}' and notation == 'plain' else char)
            pos += 1
        else:
            raise Unknown('unsupported_token')
        if len(tokens) > LIMITS['tokens']:
            raise Unknown('token_capacity')
    return tokens


def split_relations(tokens):
    parts, operations, depth, begin = [], [], 0, 0
    for i, token in enumerate(tokens):
        if token in OPEN:
            depth += 1
            if depth > LIMITS['depth']:
                raise Unknown('depth_capacity')
        elif token in CLOSE:
            depth -= 1
            if depth < 0:
                raise Unknown('unbalanced_delimiters')
        elif token in RELATIONS and depth == 0:
            parts.append(tokens[begin:i])
            operations.append(token)
            begin = i + 1
    if depth:
        raise Unknown('unbalanced_delimiters')
    parts.append(tokens[begin:])
    if not operations or any(not part for part in parts):
        raise Unknown('incomplete_or_no_relation')
    if len(operations) > LIMITS['relations']:
        raise Unknown('relation_capacity')
    if any(op.startswith('ambiguous') for op in operations):
        raise Unknown('strict_subset_convention_unknown')
    return parts, operations


def check_set_precedence(tokens):
    """Require parentheses for difference mixed with other set operators or
    repeated difference. No unstated associativity convention is inferred.
    """
    groups = [[]]
    def check(group):
        if 'difference' in group and (len(set(group)) > 1 or group.count('difference') > 1):
            raise Unknown('mixed_difference_precedence_unknown')
    for token in tokens:
        if token in OPEN:
            groups.append([])
        elif token in CLOSE:
            if len(groups) == 1:
                raise Unknown('unbalanced_delimiters')
            check(groups.pop())
        elif token in {'union', 'intersect', 'difference'}:
            groups[-1].append(token)
    for group in groups:
        check(group)


class Budget:
    def __init__(self):
        self.nodes = self.atoms = self.points = 0
        self.used_intervals = False

    def tick(self):
        self.nodes += 1
        if self.nodes > LIMITS['nodes']:
            raise Unknown('node_capacity')

    def rational(self, value):
        self.tick()
        if max(abs(value.numerator).bit_length(), value.denominator.bit_length()) > LIMITS['rational_bits']:
            raise Unknown('rational_capacity')
        return value

    def atom(self, points=0):
        self.atoms += 1
        self.points += points
        self.tick()
        if self.atoms > LIMITS['set_atoms'] or self.points > LIMITS['finite_points']:
            raise Unknown('set_capacity')


def as_sympy(value):
    return Rational(value.numerator, value.denominator)


class Parser:
    def __init__(self, tokens, budget):
        self.tokens, self.budget, self.position = tokens, budget, 0

    def peek(self):
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self, token=None):
        current = self.peek()
        if current is None or (token is not None and current != token):
            raise Unknown('incomplete_or_invalid_expression')
        self.position += 1
        return current

    def number(self):
        value = self.term()
        while self.peek() in {'+', '-'}:
            op = self.take()
            other = self.term()
            value = self.budget.rational(value + other if op == '+' else value - other)
        return value

    def term(self):
        value = self.factor()
        while self.peek() in {'*', '/'}:
            op = self.take()
            other = self.factor()
            if op == '/' and other == 0:
                raise Unknown('division_by_zero')
            value = self.budget.rational(value * other if op == '*' else value / other)
        return value

    def factor(self):
        self.budget.tick()
        token = self.take()
        if token in {'+', '-'}:
            value = self.factor()
            return value if token == '+' else -value
        if token in {'(', '{'}:
            value = self.number()
            self.take(')' if token == '(' else '}')
            return value
        if token == 'frac':
            self.take('{')
            numerator = self.number()
            self.take('}')
            self.take('{')
            denominator = self.number()
            self.take('}')
            if denominator == 0:
                raise Unknown('division_by_zero')
            return self.budget.rational(numerator / denominator)
        if NUMBER.fullmatch(token):
            return self.budget.rational(Fraction(token))
        raise Unknown('non_numeric_expression')

    def set_expression(self):
        value = self.set_intersection()
        while self.peek() == 'union':
            self.take()
            value = value.union(self.set_intersection())
            self.budget.tick()
        return value

    def set_intersection(self):
        value = self.set_atom()
        operations = set()
        while self.peek() in {'intersect', 'difference'}:
            op = self.take()
            operations.add(op)
            if len(operations) > 1:
                raise Unknown('mixed_difference_precedence_unknown')
            other = self.set_atom()
            value = value.intersect(other) if op == 'intersect' else value - other
            self.budget.tick()
        return value

    def outer_comma(self):
        depth = 0
        for token in self.tokens[self.position:]:
            if token in OPEN:
                depth += 1
            elif token in CLOSE:
                depth -= 1
                if depth == 0:
                    return False
            elif token == ',' and depth == 1:
                return True
        return False

    def set_atom(self):
        token = self.peek()
        if token == 'empty':
            self.take()
            self.budget.atom()
            return S.EmptySet
        if token == 'set{':
            self.take()
            points = []
            if self.peek() != 'set}':
                while True:
                    points.append(as_sympy(self.number()))
                    if self.budget.points + len(points) > LIMITS['finite_points']:
                        raise Unknown('set_capacity')
                    if self.peek() != ',':
                        break
                    self.take(',')
            self.take('set}')
            self.budget.atom(len(points))
            return FiniteSet(*points)
        if token == '(' and not self.outer_comma():
            self.take('(')
            value = self.set_expression()
            self.take(')')
            return value
        if token in {'[', '('}:
            self.budget.used_intervals = True
            left_open = self.take() == '('
            left = as_sympy(self.number())
            self.take(',')
            right = as_sympy(self.number())
            close = self.take()
            if close not in {']', ')'}:
                raise Unknown('invalid_interval')
            self.budget.atom()
            return Interval(left, right, left_open=left_open, right_open=close == ')')
        raise Unknown('unsupported_set_expression')

    def object(self):
        is_set = any(token in {'[', 'set{', 'empty', 'intersect', 'union', 'difference'}
                     for token in self.tokens) or ',' in self.tokens
        if is_set:
            check_set_precedence(self.tokens)
        value = self.set_expression() if is_set else self.number()
        if self.peek() is not None:
            raise Unknown('unconsumed_expression')
        return ('set' if is_set else 'rational'), value


def set_description(value):
    if value is S.EmptySet:
        return {'type': 'empty'}
    if isinstance(value, FiniteSet):
        return {'type': 'finite', 'points': [str(v) for v in value.args]}
    if isinstance(value, Interval):
        return {'type': 'real_interval', 'left': str(value.start), 'right': str(value.end),
                'left_open': bool(value.left_open), 'right_open': bool(value.right_open)}
    if isinstance(value, Union):
        return {'type': 'union', 'components': [set_description(v) for v in value.args]}
    raise Unknown('unevaluated_set')


def witness(value):
    if isinstance(value, FiniteSet):
        return value.args[0]
    if isinstance(value, Interval):
        return (value.start + value.end) / 2
    if isinstance(value, Union):
        for component in value.args:
            point = witness(component)
            if point is not None:
                return point
    return None


def set_comparison(left, op, right):
    forward, reverse = left - right, right - left
    a, b = forward.is_empty, reverse.is_empty
    if a is None or b is None:
        raise Unknown('set_emptiness_unknown')
    truth = {'=': a and b, '!=': not (a and b), 'subseteq': a,
             'supseteq': b, 'subsetneq': a and not b, 'supsetneq': b and not a}.get(op)
    if truth is None:
        raise Unknown('relation_type_mismatch')
    evidence = {'left_minus_right': set_description(forward), 'right_minus_left': set_description(reverse)}
    for name, difference in [('left_minus_right', forward), ('right_minus_left', reverse)]:
        if difference.is_empty is False:
            point = witness(difference)
            if point is None or difference.contains(point) is not S.true:
                raise Unknown('witness_unverified')
            memberships = left.contains(point), right.contains(point)
            if any(v is not S.true and v is not S.false for v in memberships):
                raise Unknown('witness_membership_unknown')
            evidence[name + '_witness'] = {'point': str(point),
                'in_left': memberships[0] is S.true, 'in_right': memberships[1] is S.true}
    return bool(truth), evidence


def verify_relation(text, *, notation='plain', interval_domain=None):
    result = {'original': text, 'status': 'unknown', 'scope': 'local_explicit_objects_only',
              'mathematical_correctness_verified': False, 'interval_domain': interval_domain,
              'assumptions': ['Numeric literals denote their standard exact rational values.']}
    if not isinstance(text, str):
        result['reason'] = 'text_required'
        return result
    try:
        budget = Budget()
        parts, operations = split_relations(tokenize(text, notation))
        objects = [Parser(part, budget).object() for part in parts]
        if budget.used_intervals:
            if interval_domain != 'real':
                raise Unknown('real_interval_domain_not_declared')
            result['assumptions'].append('Bracketed pairs denote intervals of the real line, not tuples or commutators.')
        if len({kind for kind, _ in objects}) != 1:
            raise Unknown('object_type_mismatch')
        checks = []
        for i, op in enumerate(operations):
            kind, left = objects[i]
            right = objects[i + 1][1]
            if kind == 'set':
                truth, evidence = set_comparison(left, op, right)
                evidence.update(left=set_description(left), right=set_description(right))
            else:
                if op not in {'=', '!=', '<', '<=', '>', '>='}:
                    raise Unknown('relation_type_mismatch')
                truth = {'=': left == right, '!=': left != right, '<': left < right,
                         '<=': left <= right, '>': left > right, '>=': left >= right}[op]
                evidence = {'left': str(left), 'right': str(right), 'left_minus_right': str(left - right)}
            checks.append({'operation': op, 'holds': truth, 'evidence': evidence})
        result.update(status='matches' if all(c['holds'] for c in checks) else 'contradicts', checks=checks)
    except (Unknown, ZeroDivisionError, ValueError, TypeError, RecursionError) as exc:
        result.update(status='unknown', reason=str(exc) if isinstance(exc, Unknown) else 'backend_or_parse_failure')
    return result


def math_spans(text, excluded):
    """Reject unmatched/mixed delimiters rather than recover a literal suffix."""
    masked = list(text)
    for a, b in excluded:
        masked[a:b] = ' ' * (b - a)
    markers = list(DELIMITER.finditer(''.join(masked)))
    spans, unknown, i = [], [], 0
    closers = {'$': '$', '$$': '$$', '\\[': '\\]', '\\(': '\\)'}
    while i < len(markers):
        opening = markers[i]
        expected = closers.get(opening.group())
        if expected is None or i + 1 == len(markers) or markers[i + 1].group() != expected:
            unknown.append({'start': opening.start(), 'end': len(text),
                            'reason': 'unbalanced_or_ambiguous_math_delimiters'})
            break
        closing = markers[i + 1]
        spans.append((opening.start(), closing.end(), len(opening.group())))
        i += 2
    return spans, unknown


def narrative_context(text, start, end):
    begin = text.rfind('\n\n', 0, start)
    begin = 0 if begin < 0 else begin + 2
    finish = text.find('\n\n', end)
    finish = len(text) if finish < 0 else finish
    # List items delimit separate assertions; a preceding item saying "not open"
    # is not a negation of this item's equality. Multi-line formulas stay intact.
    item = re.compile(r'(?m)^\s*(?:\d+[.)、]|[-*])\s+')
    for match in item.finditer(text, begin, start):
        begin = match.start()
    next_item = item.search(text, end, finish)
    if next_item:
        finish = next_item.start()
    return text[begin:start] + text[end:finish]


def inspect_document(text, *, scopes=()):
    """Inspect whole math spans. Explicit interval scopes are supplied by a caller.

    Context heuristics never certify assertion roles; supported computations also
    retain this limitation. Scope declarations establish notation, not truth.
    """
    report = {'version': VERSION, 'source_sha256': hashlib.sha256(text.encode()).hexdigest(),
              'records': [], 'unknown_ranges': [], 'limits': LIMITS,
              'mathematical_correctness_verified': False, 'whole_document_coverage': 'unknown',
              'note': 'Local object calculations only; contextual assertion truth and full proof remain unverified.'}
    if len(text) > LIMITS['document_characters']:
        report['unknown_ranges'].append({'start': 0, 'end': len(text), 'reason': 'document_capacity'})
        return report
    for scope in scopes:
        if (not isinstance(scope, dict) or scope.get('interval_domain') != 'real'
                or not isinstance(scope.get('span'), list) or len(scope['span']) != 2
                or any(type(v) is not int for v in scope['span'])
                or not 0 <= scope['span'][0] < scope['span'][1] <= len(text)):
            raise ValueError('Invalid explicit interval scope')
    excluded = [(m.start(), m.end()) for m in EXCLUDED.finditer(text)]
    spans, unknown = math_spans(text, excluded)
    report['unknown_ranges'].extend(unknown)
    for start, end, width in spans:
        if len(report['records']) + len(report['unknown_ranges']) >= LIMITS['document_records']:
            report['unknown_ranges'].append({'start': start, 'end': len(text), 'reason': 'record_capacity'})
            break
        raw = text[start:end]
        inner = raw[width:-width]
        if not RELATION_CUE.search(inner):
            report['unknown_ranges'].append({'start': start, 'end': end, 'reason': 'no_complete_relation'})
            continue
        context = narrative_context(text, start, end)
        declarations = [scope for scope in scopes if scope['span'][0] <= start
                        and end <= scope['span'][1]]
        if CONTEXT.search(context):
            checked = {'original': inner, 'status': 'unknown', 'reason': 'assertion_context_unknown',
                       'mathematical_correctness_verified': False}
        else:
            checked = verify_relation(inner, notation='latex', interval_domain='real' if declarations else None)
        checked.update(start=start, end=end, raw=raw,
                       assertion_role_verified=False, interval_scope_declared=bool(declarations))
        report['records'].append(checked)
    return report
