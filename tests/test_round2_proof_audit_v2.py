"""Offline audit contracts: no requests to an external service in this file."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.prepare_proof_audit_v2 import INDICES, prepare
from benchmarks.run_proof_audit_v2 import (install_durable_http_cap, ledger_count,
    load_pack, main, run_case, validate_history)
from config import CONFIG
from utils.call_recording import HttpBudgetExceeded
from utils.proof_audit import audit_messages, audit_once, parse_audit, quote_locations
from utils.run_manifest import atomic_json, ensure_manifest, sha256
from utils.score_budget import ScoreBudget
from utils.wall_clock import TimeBudget

ANSWER = "## 正文\n等式成立。\n## 结论\n等式成立。\n"


def record(**changes):
    return {"quote": "等式成立。", "section": "## 正文", "premises": "实数条件", "status": "supported",
            "basis": "由定义逐项相等", **changes}


def response(**changes):
    return json.dumps({"status": "no_issue_found", "issues": [], "checks": [record()], **changes}, ensure_ascii=False)


class Client:
    def __init__(self, value=None):
        self.calls = []
        self.value = response() if value is None else value
        self._tls = SimpleNamespace(finish_reason="stop", usage={"prompt_tokens": 30, "completion_tokens": 40}, http_attempts=1)

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def test_fresh_messages_have_only_public_material_and_generic_methods():
    messages = audit_messages("原题：证明实数公式。", ANSWER)
    payload = json.loads(messages[1]["content"])
    assert set(payload) == {"problem", "final_response", "advisory_methods"}
    assert payload["final_response"] == ANSWER
    assert all(m["role"] in {"system", "user"} for m in messages)
    assert "不重写答案" in messages[0]["content"]
    assert payload["advisory_methods"]["mathematical_correctness_verified"] is False


def test_exact_quote_is_disambiguated_by_section_and_never_certifies_math():
    parsed = parse_audit(response(), ANSWER, "stop")
    item = parsed["checks"][0]
    assert len(item["locations"]) == 2
    assert item["location"]["line"] == 2
    assert ANSWER[item["location"]["start"]:item["location"]["end"]] == "等式成立。"
    assert parsed["record_status"] == "parsed_unverified"
    assert parsed["mathematical_correctness_verified"] is False
    assert item["mathematical_correctness_verified"] is False


@pytest.mark.parametrize("changes,flag", [
    ({"quote": "等式 成立。"}, "quote_not_found"),
    ({"section": "正文"}, "section_mismatch"),
    ({"basis": ""}, "missing_basis"),
    ({"premises": ""}, "missing_premises"),
    ({"quote": ""}, "missing_quote"),
    ({"status": []}, "invalid_status"),
])
def test_invalid_opinion_is_retained_as_uncertain(changes, flag):
    original = record(**changes)
    parsed = parse_audit(response(checks=[original]), ANSWER, "stop")
    assert flag in parsed["checks"][0]["flags"]
    assert parsed["checks"][0]["original"] == original
    assert parsed["record_status"] == "uncertain"


def test_repeated_quote_in_same_section_is_ambiguous():
    parsed = parse_audit(response(), "## 正文\n等式成立。等式成立。", "stop")
    assert "ambiguous_quote" in parsed["checks"][0]["flags"]
    assert "location" not in parsed["checks"][0]


@pytest.mark.parametrize("raw", ['{"status":', '[]', '{"status": [], "issues": [3], "checks": [{}]}'])
def test_malformed_output_keeps_raw_without_repair_or_exception(raw):
    parsed = parse_audit(raw, ANSWER, "length")
    assert parsed["raw_response"] == raw
    assert parsed["record_status"] == "uncertain"
    assert "truncated" in parsed["flags"]


def test_unknown_stops_and_inconsistent_status_remain_uncertain():
    assert "stop_reason_unknown" in parse_audit(response(), ANSWER)["flags"]
    parsed = parse_audit(response(status="issue_found"), ANSWER, "stop")
    assert "inconsistent_status" in parsed["flags"]


def test_located_false_mathematical_basis_is_still_unverified():
    parsed = parse_audit(response(checks=[record(basis="0=1，所以所有公式正确")]), ANSWER, "stop")
    assert parsed["record_status"] == "parsed_unverified"  # Shape/location only, deliberately not a truth oracle.
    assert not parsed["mathematical_correctness_verified"]


def test_one_call_uses_unchanged_parameters_and_preserves_answer():
    client, tokens = Client(), ScoreBudget()
    result = audit_once(client, "证明原题", ANSWER, time_budget=TimeBudget(), token_budget=tokens)
    assert len(client.calls) == 1
    assert client.calls[0]["temperature"] == CONFIG["temperatures"]["coordinator"]
    assert client.calls[0]["max_tokens"] == CONFIG["max_tokens"]["coordinator"]
    assert result["final_response"] == ANSWER and tokens.consumed == 70
    assert result["usage"] == client._tls.usage
    assert result["status"] == "audit_received"


@pytest.mark.parametrize("raw", ["invalid JSON", response()])
def test_truncation_or_parse_failure_never_gets_semantic_retry(raw):
    client = Client(raw)
    client._tls.finish_reason = "length"
    result = audit_once(client, "原题", ANSWER, time_budget=TimeBudget(), token_budget=ScoreBudget())
    assert len(client.calls) == 1 and result["final_response"] == ANSWER
    assert result["audit"]["record_status"] == "uncertain"


def test_expired_time_and_exhausted_tokens_make_no_calls():
    now = [0]
    budget = TimeBudget(clock=lambda: now[0])
    now[0] = 1201
    client = Client()
    result = audit_once(client, "原题", ANSWER, time_budget=budget, token_budget=ScoreBudget())
    assert result["status"] == "time_budget_skipped" and not client.calls
    result = audit_once(client, "原题", ANSWER, time_budget=TimeBudget(), token_budget=ScoreBudget(1))
    assert result["status"] == "token_budget_skipped" and not client.calls
    assert result["final_response"] == ANSWER


def test_transport_retries_are_bounded_and_failure_preserves_answer(monkeypatch):
    monkeypatch.setattr("utils.llm_retry_guard.time.sleep", lambda _: None)
    client = Client(RuntimeError("mock transport failure"))
    result = audit_once(client, "原题", ANSWER, time_budget=TimeBudget(), token_budget=ScoreBudget())
    assert len(client.calls) == CONFIG["llm_max_retries"]
    assert result["status"] == "transport_failed" and result["final_response"] == ANSWER


def test_official_style_client_without_metadata_remains_usable():
    client = SimpleNamespace(chat=lambda **kwargs: response())
    result = audit_once(client, "原题", ANSWER, time_budget=TimeBudget(), token_budget=ScoreBudget())
    assert result["finish_reason"] is None and result["usage"] is None
    assert result["audit"]["record_status"] == "uncertain"


def test_http_cap_survives_restart_and_charges_transport_failure(tmp_path, monkeypatch):
    import requests
    requests_seen = []
    def fail(*args, **kwargs):
        requests_seen.append(args)
        raise RuntimeError("mock failed physical attempt")
    monkeypatch.setattr(requests, "post", fail)
    ledger = tmp_path / "admissions.jsonl"
    install_durable_http_cap(ledger, 4, 1)
    with pytest.raises(RuntimeError):
        requests.post("mock://no-network")
    assert ledger_count(ledger) == 1
    install_durable_http_cap(ledger, 5, 1)
    with pytest.raises(HttpBudgetExceeded):
        requests.post("mock://no-network")
    assert len(requests_seen) == 1 and ledger_count(ledger) == 1


def test_corrupt_http_history_refuses_resume(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text('{"attempt": 2}\n')
    with pytest.raises(ValueError):
        ledger_count(ledger)


@pytest.mark.parametrize("attempts", [None, 1])
def test_missing_journal_or_unknown_history_refuses_resume(tmp_path, attempts):
    path = tmp_path / "calls" / "4.calls.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps({"http_attempts": attempts}) + "\n")
    with pytest.raises(ValueError):
        validate_history(tmp_path)


def test_worker_timeout_preserves_answer_and_has_finite_ceiling(tmp_path):
    args = SimpleNamespace(output_dir=tmp_path / "result", pack=tmp_path / "pack", env_file=tmp_path / "env",
                           max_http_attempts=8)
    def expire(command, **kwargs):
        assert command[1:3] == ["-X", "utf8"]
        assert kwargs["timeout"] == CONFIG["node_timeouts"]["coordinator"]
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    run_case({"idx": 4, "problem": "原题", "final_response": ANSWER}, args, run_process=expire)
    result = json.loads((args.output_dir / "4.json").read_text(encoding="utf-8"))
    assert result["status"] == "audit_timeout" and result["final_response"] == ANSWER
    assert result["completed"] and result["server_completion_unknown"]


@pytest.mark.parametrize("failure", ["launch", "crash"])
def test_worker_launch_or_process_failure_preserves_answer(tmp_path, failure):
    args = SimpleNamespace(output_dir=tmp_path / "result", pack=tmp_path / "pack", env_file=tmp_path / "env",
                           max_http_attempts=8)
    def fail(command, **kwargs):
        if failure == "launch":
            raise OSError("mock could not start worker")
        return SimpleNamespace(returncode=2, stderr="模拟子进程异常", stdout="")
    run_case({"idx": 4, "problem": "原题", "final_response": ANSWER}, args, run_process=fail)
    result = json.loads((args.output_dir / "4.json").read_text(encoding="utf-8"))
    assert result["status"] == ("worker_launch_failed" if failure == "launch" else "worker_failed")
    assert result["completed"] and result["final_response"] == ANSWER


@pytest.fixture
def frozen_pack(tmp_path):
    root, source, out = tmp_path / "source_code", tmp_path / "run", tmp_path / "pack"
    root.mkdir()
    (root / "stub.py").write_text("# fake frozen code\n")
    source.mkdir()
    (source / "tested_source.zip").write_bytes(b"fake source provenance")
    source_code = {"commit": "mock_commit", "dirty": False}
    manifest = {"inputs": [{"idx": idx, "problem": f"原题{idx}"} for idx in INDICES],
                "code": source_code, "config": CONFIG, "model": CONFIG["model"], "agent_version": "mock"}
    atomic_json(source / "run.manifest.json", manifest)
    atomic_json(source / "tested_source.manifest.json", {"code": source_code,
                "source_archive_sha256": sha256((source / "tested_source.zip").read_bytes())})
    review = {"items": {}}
    for idx in INDICES:
        atomic_json(source / f"{idx}.json", {"status": "success", "final_response": ANSWER,
                    "trace": [{"draft": "SECRET_DRAFT"}], "reference": "SECRET_REFERENCE"})
        review["items"][str(idx)] = {"final_response_sha256": sha256(ANSWER.encode()), "label": "SECRET_LABEL"}
    manual_path = tmp_path / "human.json"
    atomic_json(manual_path, review)
    prepare(source, manual_path, out, root=root)
    return out, root, source, manual_path


def test_frozen_material_separates_labels_and_preserves_full_answers(frozen_pack):
    pack, root, _, _ = frozen_pack
    rows, manifest = load_pack(pack, root=root)
    assert [r["idx"] for r in rows] == INDICES
    assert all(r["final_response"] == ANSWER for r in rows)
    for path in [pack / "input.jsonl", *(pack / "prompts").glob("*.json")]:
        text = path.read_text(encoding="utf-8")
        assert not any(secret in text for secret in ("SECRET_LABEL", "SECRET_REFERENCE", "SECRET_DRAFT"))
    assert "SECRET_LABEL" in (pack / "manual_only.json").read_text()
    assert manifest["default_workflow_modified"] is False


@pytest.mark.parametrize("filename", ["input.jsonl", "prompts/4.json", "source.zip"])
def test_changed_pack_is_rejected(frozen_pack, filename):
    pack, root, _, _ = frozen_pack
    with (pack / filename).open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError):
        load_pack(pack, root=root)


def test_changed_runtime_is_rejected_and_freeze_cannot_overwrite(frozen_pack):
    pack, root, source, review = frozen_pack
    (root / "stub.py").write_text("changed\n")
    with pytest.raises(ValueError):
        load_pack(pack, root=root)
    with pytest.raises(ValueError):
        prepare(source, review, pack, root=root)


def test_added_source_file_is_rejected(frozen_pack):
    pack, root, _, _ = frozen_pack
    (root / "new.py").write_text("# extra code\n")
    with pytest.raises(ValueError):
        load_pack(pack, root=root)


def test_reference_field_is_rejected_even_with_updated_input_hash(frozen_pack):
    pack, root, _, _ = frozen_pack
    path = pack / "input.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["reference"] = "answer"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    manifest_path = pack / "pack.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["input_sha256"] = sha256(path.read_bytes())
    atomic_json(manifest_path, manifest)
    with pytest.raises(ValueError, match="Only idx"):
        load_pack(pack, root=root)


def test_resume_does_not_repeat_failed_or_malformed_audits(frozen_pack, monkeypatch, tmp_path):
    import benchmarks.run_proof_audit_v2 as runner
    import llm_client
    pack, root, _, _ = frozen_pack
    original_loader = load_pack
    monkeypatch.setattr(runner, "load_pack", lambda p: original_loader(p, root=root))
    monkeypatch.setattr(runner, "load_env", lambda p: None)
    monkeypatch.setenv("INTERN_MODEL", CONFIG["model"])
    monkeypatch.setenv("INTERN_API_BASE", llm_client.DEFAULT_API_BASE)
    completed = []
    def finish(row, args):
        completed.append(row["idx"])
        atomic_json(args.output_dir / f"{row['idx']}.json", {"idx": row["idx"], "completed": True,
                    "final_response": row["final_response"], "status": "transport_failed"})
    monkeypatch.setattr(runner, "run_case", finish)
    monkeypatch.setattr("sys.argv", ["audit", "--pack", str(pack), "--output-dir", str(tmp_path / "audit_run"), "--run"])
    main()
    main()
    assert completed == INDICES
    summary = json.loads((tmp_path / "audit_run/summary.json").read_text(encoding="utf-8"))
    assert summary["audit_jobs_completed"] == 5 and summary["http_attempts_admitted"] == 0


def test_resume_identity_rejects_changed_budget(tmp_path):
    path = tmp_path / "manifest.json"
    ensure_manifest(path, {"created_at": "old", "input_hash": "same", "cap": 8})
    ensure_manifest(path, {"created_at": "new", "input_hash": "same", "cap": 8})
    with pytest.raises(ValueError):
        ensure_manifest(path, {"created_at": "new", "input_hash": "same", "cap": 9})


def test_default_cli_preflight_cannot_invoke_client_or_worker(frozen_pack, monkeypatch, capsys, tmp_path):
    import llm_client
    import benchmarks.run_proof_audit_v2 as runner
    pack, root, _, _ = frozen_pack
    original_loader = load_pack
    monkeypatch.setattr(runner, "load_pack", lambda p: original_loader(p, root=root))
    def forbidden(*args, **kwargs):
        raise AssertionError("preflight attempted a model request")
    monkeypatch.setattr(llm_client, "InternChatClient", forbidden)
    monkeypatch.setattr(runner, "worker", forbidden)
    monkeypatch.setattr(runner, "run_case", forbidden)
    monkeypatch.setattr(runner, "load_env", lambda p: None)
    monkeypatch.setattr("sys.argv", ["audit", "--pack", str(pack), "--output-dir", str(tmp_path / "unused")])
    main()
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run_no_api"
    assert not (tmp_path / "unused").exists()
