"""Observable calls, resume identity and client isolation regressions."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval_local import LocalClient
from llm_client import InternChatClient
from utils.call_recording import RecordedClient
from utils.diagnostics import python_diagnostics, call_counts
from utils.llm_retry_guard import LLMRetryGuard, DeadlineExceeded
from utils.run_manifest import ensure_manifest
from utils.wall_clock import TimeBudget


def test_evaluation_uses_same_client_and_isolates_transport_metadata(monkeypatch):
    barrier = threading.Barrier(2)
    def post(url, headers, data, timeout):
        token = json.loads(data)["messages"][0]["content"]
        barrier.wait(timeout=5)
        return SimpleNamespace(raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": token}, "finish_reason": token}],
                          "usage": {"completion_tokens": len(token)}})
    monkeypatch.setattr("requests.post", post)
    client = LocalClient({"INTERN_API_KEY": "test-key"})
    assert isinstance(client, InternChatClient)
    def run(token):
        result = LLMRetryGuard(client).chat([{"role": "user", "content": token}])
        return str(result), result.finish_reason, result.usage
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(run, ("stop", "length")))
    assert values == [("stop", "stop", {"completion_tokens": 4}),
                      ("length", "length", {"completion_tokens": 6})]


def test_transport_failure_clears_prior_usage_and_counts_http_attempts(monkeypatch):
    client = InternChatClient(api_key="test-key", retry=1)
    client._tls.usage = {"old": True}
    client._tls.finish_reason = "stop"
    def fail(*args, **kwargs):
        raise RuntimeError("gateway down")
    monkeypatch.setattr("requests.post", fail)
    with pytest.raises(RuntimeError):
        client.chat([])
    assert client._tls.usage is None
    assert client._tls.finish_reason is None
    assert client._tls.http_attempts == 1


def test_local_call_capture_does_not_write_authorization(tmp_path):
    client = SimpleNamespace(_tls=SimpleNamespace(http_attempts=1, finish_reason="stop", usage=None),
                             authorization="private-value", chat=lambda **kwargs: "visible answer")
    recorded = RecordedClient(client, tmp_path, 5)
    assert recorded.chat([{"role": "user", "content": "problem"}]) == "visible answer"
    text = (tmp_path / "5.calls.jsonl").read_text(encoding="utf-8")
    assert "private-value" not in text
    assert json.loads(text)["http_attempts"] == 1


@pytest.mark.parametrize("key,value", [("commit", "new"), ("model", "new"), ("input_sha256", "changed"),
                                      ("concurrency", 2), ("config", {"temperature": 0.9})])
def test_resume_rejects_changed_identity(tmp_path, key, value):
    manifest = {"created_at": "old", "commit": "base", "model": "model", "input_sha256": "same",
                "concurrency": 1, "config": {"temperature": 0.2}}
    path = tmp_path / "run.manifest.json"
    ensure_manifest(path, manifest)
    ensure_manifest(path, {**manifest, "created_at": "later"}, has_results=True)
    with pytest.raises(ValueError, match="identity changed"):
        ensure_manifest(path, {**manifest, key: value}, has_results=True)


def test_legacy_results_are_readable_but_not_silently_resumed(tmp_path):
    with pytest.raises(ValueError, match="lack a run manifest"):
        ensure_manifest(tmp_path / "manifest.json", {"created_at": "now"}, has_results=True)


def test_skips_and_failures_are_separate():
    assert python_diagnostics({"python_output": {"skipped": True}}) == {"strategy_skipped": 1}
    assert python_diagnostics({"python_trace": [{"failure_kind": "syntax_failed"}],
                               "python_output": {"success": True, "evidence_status": "inconclusive"}}) == {
                                   "syntax_failed": 1, "execution_success": 1, "evidence_uncertain": 1}


def test_only_real_model_attempts_count_and_platform_http_remains_unknown():
    clock = TimeBudget()
    clock.record("node:reasoning", 4)
    clock.record("reasoning", 550)  # estimated cost of a timed-out call is not another request
    clock.record("reasoning:failed", 1, model_call=True, http_attempts=2)
    clock.record("reasoning", 2, model_call=True, http_attempts=1)
    assert call_counts(clock.spend_log()) == {"logical_model_calls": 2, "http_attempts": 3}
    clock.record("arbiter", 1, model_call=True)
    assert call_counts(clock.spend_log())["http_attempts"] is None


def test_deadline_does_not_buy_a_call():
    client = SimpleNamespace(chat=lambda **kwargs: pytest.fail("deadline must skip the request"))
    clock = TimeBudget(total_seconds=1, reserve_seconds=1)
    with pytest.raises(DeadlineExceeded):
        LLMRetryGuard(client, time_budget=clock).chat([])
    assert call_counts(clock.spend_log())["logical_model_calls"] == 0


def test_http_budget_blocks_before_transport_and_binding_keeps_recorder(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("requests.post", lambda *args, **kwargs: sent.append(True))
    from utils.call_recording import install_http_probe, HttpBudgetExceeded
    import requests
    probe = install_http_probe(2)
    requests.post("mock")
    requests.post("mock")
    with pytest.raises(HttpBudgetExceeded):
        requests.post("mock")
    assert len(sent) == 2 and probe.count() == 2 and probe.exhausted()
    client = InternChatClient(api_key="test")
    recording = RecordedClient(client, tmp_path, 1, probe)
    assert recording.for_problem(1) is recording


def test_full_proof_pipeline_preserves_body_and_records_all_calls(tmp_path, monkeypatch):
    pytest.importorskip("langgraph")
    body = "取任意闭集 F。它在紧致空间中紧致，连续像紧致；目标 Hausdorff，所以像闭。因此逆映射连续，映射是同胚。"
    reasoning = ("## 结论速览\n映射是同胚。\n## 问题分析\n用闭映射证明。\n"
                 "## 详细解题步骤\n" + body + "\n## 最终答案\n映射是同胚。\n## 关键验证点\n- 核查所有前提。")
    replies = iter(['拓扑学", "confidence": 0.95}', reasoning,
                    body + '\n<!-- PROOF_REVIEW {"status":"none_reported","issues":[]} -->'])
    def post(*args, **kwargs):
        content = next(replies)
        return SimpleNamespace(raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                          "usage": {"completion_tokens": 100}})
    monkeypatch.setattr("requests.post", post)
    from user_agent import ReasoningAgent
    client = InternChatClient(api_key="test")
    client.record_dir = tmp_path
    result = ReasoningAgent(client).solve("证明紧致空间到 Hausdorff 空间的连续双射是同胚。", {"idx": 42})
    assert body in result["final_response"]
    calls = [json.loads(line) for line in (tmp_path / "42.calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 3
    diagnostic = next(entry for entry in result["trace"] if entry["step"] == "diagnostics")
    assert diagnostic["logical_model_calls"] == diagnostic["http_attempts"] == 3
    assert diagnostic["python"] == {"strategy_skipped": 1}
    assert "PROOF_REVIEW" not in result["final_response"]
    json.dumps(result)
