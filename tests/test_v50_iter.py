# -*- coding: utf-8 -*-
"""V5.0：验证失败反例反馈提取器单元测试。"""
import sys

import pytest

sys.path.insert(0, ".")  # noqa: E402

from utils.verifier_feedback import (  # noqa: E402
    extract_feedback,
    extract_instance_feedback,
)


class TestFeedbackExtractor:
    def test_extract_counterexample(self):
        stdout = """
        Testing candidate...
        n=2: candidate=7, expected=5  (brute force gives 5)
        n=3: candidate=15, expected=13
        VERIFY: FAIL
        """
        fb = extract_feedback(stdout)
        assert "验证器反馈" in fb
        assert "n=2" in fb
        assert "FAIL" not in fb  # 不把判定行当反例反馈

    def test_extract_no_feedback(self):
        assert extract_feedback("VERIFY: PASS\nall good") == ""
        assert extract_feedback("") == ""

    def test_extract_stderr_fallback(self):
        fb = extract_feedback("", "n=1 mismatch: got 3 expected 2")
        assert "mismatch" in fb

    def test_instance_feedback(self):
        stdout = "n=2: candidate=7, expected=5\na=3,b=1: 不成立\nVERIFY: FAIL"
        inst = extract_instance_feedback(stdout)
        assert "n=2" in inst or "a=3" in inst

    def test_dedup(self):
        stdout = "n=1: candidate=3, expected=2\nn=1: candidate=3, expected=2\nVERIFY: FAIL"
        fb = extract_feedback(stdout)
        assert fb.count("n=1: candidate=3, expected=2") == 1


class TestIterSolverStructure:
    def test_solver_has_iteration(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dw", "local_test/deep_water_solver.py")
        # 只做静态结构检查（不执行）
        src = open("local_test/deep_water_solver.py", encoding="utf-8").read()
        assert "max_rounds" in src
        assert "feedback" in src
        assert "_sample_round" in src
        assert "verify_candidate" in src
        assert "unconfirmed" in src
