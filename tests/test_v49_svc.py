# -*- coding: utf-8 -*-
"""V4.9：竞赛方法论路由 + 验证器 + SVC 锁定逻辑单元测试。"""
import sys

import pytest

sys.path.insert(0, ".")  # noqa: E402

from utils.competition_methods import (  # noqa: E402
    route_competition_method,
    competition_hint,
    method_name,
)


class TestCompetitionRouting:
    def test_game_routes_sg(self):
        p = "Alice and Bob play a game. Who has a winning strategy?"
        assert "SG" in route_competition_method(p)
        assert method_name(p) == "sg_game"

    def test_counting_routes_genfunc(self):
        p = "How many ways are there to arrange 5 red and 3 blue balls?"
        assert "生成函数" in route_competition_method(p)

    def test_geometry_routes_complex(self):
        p = "In triangle ABC, the incircle touches sides at D, E, F. Find angle DEF."
        assert "复数" in route_competition_method(p)

    def test_number_theory_routes(self):
        p = "Find all primes p such that p divides 2^p - 2."
        assert "数论" in route_competition_method(p)

    def test_extremal_routes(self):
        p = "Find the maximum value of x+y+z subject to xyz=1."
        assert "极值" in route_competition_method(p)

    def test_invariant_routes(self):
        p = "A board is colored black and white. In one operation, flip all cells in a row."
        assert "不变量" in route_competition_method(p)

    def test_empty_problem(self):
        assert route_competition_method("") == ""

    def test_hint_nonempty(self):
        assert competition_hint("Find the maximum of f(x).") != ""


class TestVerifierHelpers:
    def test_parse_verdict(self):
        from utils.answer_verifier import _parse_verdict
        # V5.0-fix：PASS 必须带数值对照证据行
        assert _parse_verdict("n=1: cand=7, actual=7\nVERIFY: PASS\n") == "PASS"
        assert _parse_verdict("VERIFY: PASS\n") == "INCONCLUSIVE"  # 无证据不 PASS
        assert _parse_verdict("VERIFY: FAIL - counterexample found") == "FAIL"
        assert _parse_verdict("nothing") == "INCONCLUSIVE"

    def test_extract_code(self):
        from utils.answer_verifier import _extract_code
        assert "print(1)" in _extract_code("```python\nprint(1)\n```")
        assert _extract_code("no code here") == ""

    def test_verify_candidate_empty(self):
        from utils.answer_verifier import verify_candidate
        assert verify_candidate(None, None, "p", "")["verified"] is False
