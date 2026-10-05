"""Concurrency guard for resumable, HTTP-capped Round 4 runs."""

import importlib.util
from pathlib import Path

import pytest


def _runner_module():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("round4_runner_under_test", root / "benchmarks" / "run_round2.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_active_output_directory_rejects_concurrent_resume(tmp_path):
    runner = _runner_module()
    first = runner.acquire_run_lease(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="refusing concurrent resume"):
            runner.acquire_run_lease(tmp_path)
    finally:
        first.release()
    second = runner.acquire_run_lease(tmp_path)
    second.release()
