"""Narrow explicit scalar-domain support; unsupported restrictions stay unknown."""
import re


def explicit_domains(problem):
    # Restrict extraction to mathematical spans or explicit assumption clauses.
    spans = []
    for match in re.finditer(r"\$([^$]+)\$", problem or ""):
        prefix = (problem or "")[max(0, match.start() - 45):match.start()]
        # A desired conclusion such as 'prove x>0' must not become an assumption.
        if re.search(r"证明|(?i:prove|show)", prefix) and not re.search(
                r"其中|满足|(?:^|\s)(?i:for|where|when)\b", prefix):
            continue
        spans.append(match.group(1))
    spans += re.findall(r"(?:其中|设|满足|(?i:where|for|when))\s*([^。\n]{1,100})", problem or "")
    domains = {}
    for span in spans:
        span = span.replace(r"\geq", ">=").replace(r"\ge", ">=").replace("≥", ">=")
        span = span.replace(r"\leq", "<=").replace(r"\le", "<=").replace("≤", "<=")
        for match in re.finditer(r"(?<![A-Za-z0-9_])([A-Za-z])\s*(>=|<=|>|<)\s*0(?![\d.])", span):
            name, comparison = match.groups()
            assumption = {">": "positive", ">=": "nonnegative", "<": "negative", "<=": "nonpositive"}[comparison]
            if name in domains and domains[name] != assumption:
                return {}  # incompatible or combined restrictions are unsupported
            domains[name] = assumption
    return domains


def domain_equivalent(problem, left, right):
    """Only symbolically established equality; no samples certify equivalence."""
    import sympy as sp
    from utils.answer_compare import _try_parse_expr
    domains = explicit_domains(problem)
    if not domains:
        return None
    a, b = _try_parse_expr(left), _try_parse_expr(right)
    if a is None or b is None or isinstance(a, sp.MatrixBase) or isinstance(b, sp.MatrixBase):
        return None
    symbols = a.free_symbols | b.free_symbols
    replacements = {symbol: sp.Symbol(str(symbol), **{domains[str(symbol)]: True})
                    for symbol in symbols if str(symbol) in domains}
    if not replacements:
        return None
    a, b = a.xreplace(replacements), b.xreplace(replacements)
    if any(value.has(sp.nan, sp.zoo, sp.oo, -sp.oo) for value in (a, b)):
        return None
    try:
        difference = sp.simplify(a - b)
        if difference == 0:
            return True
        # Safe exact constant disagreement; arbitrary sampled points may be outside
        # the stated domain, so free-symbol disagreement remains uncertain here.
        if not difference.free_symbols and difference.is_zero is False and difference.is_finite is True:
            return False
    except Exception:
        pass
    return None
