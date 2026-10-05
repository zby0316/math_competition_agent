import json

import pytest

from main import load_jsonl, result_path
from utils.call_recording import HttpBudgetExceeded, RecordedClient, install_http_probe
from utils.exec_client import ExecClient
from exec_server.executor import execute_python
from utils.safe_paths import output_file


@pytest.mark.parametrize("value", ["../escaped", "C:/outside", -1, True, None])
def test_output_identifiers_fail_closed(value, tmp_path):
    with pytest.raises(ValueError):
        output_file(tmp_path, value, ".json")


def test_main_result_path_stays_under_requested_directory(tmp_path):
    path = result_path(tmp_path, {"idx": 12})
    assert path.parent == tmp_path.resolve()
    assert path.name == "12.json"


def test_recorded_client_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        RecordedClient(object(), tmp_path, "../escaped")


def test_recorded_client_reserves_global_budget_before_calling_transport(tmp_path):
    class Client:
        _tls = type("TLS", (), {})()

        def __init__(self):
            self.calls = 0

        def chat(self, **_kwargs):
            self.calls += 1
            return "ok"

    client = Client()
    probe = install_http_probe(2)
    recorder = RecordedClient(client, tmp_path, 9, probe)
    assert recorder.chat([]) == "ok"
    assert recorder.chat([]) == "ok"
    with pytest.raises(HttpBudgetExceeded):
        recorder.chat([])
    assert client.calls == 2
    assert probe.admitted() == 2
    assert probe.exhausted() is True


def test_jsonl_loader_rejects_non_integer_identifier_and_non_string_problem(tmp_path):
    path = tmp_path / "input.jsonl"
    path.write_text(json.dumps({"idx": "../escaped", "problem": "x"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_jsonl(path)
    path.write_text(json.dumps({"idx": 1, "problem": 3}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_jsonl(path)


def test_executor_returns_answer_from_disposable_working_directory():
    result = execute_python('open("relative.txt", "w").write("x")\nprint("最终答案:", 7)')
    assert result["success"] is True
    assert result["answer"] == "7"
    assert result["isolation_level"] == "process_only"


def test_executor_preserves_visible_answer_on_timeout():
    result = execute_python('print("最终答案:", 7, flush=True)\nwhile True: pass', timeout=1)
    assert result["success"] is False
    assert result["answer"] == "7"


def test_exec_client_fails_closed_when_subprocess_path_raises(monkeypatch):
    monkeypatch.setattr("utils.exec_client._exec", lambda *_: (_ for _ in ()).throw(RuntimeError("broken")))
    result = ExecClient().execute("print(1)")
    assert result["success"] is False
    assert result["execution_backend"] == "subprocess_unavailable"
