"""Optional local per-problem visible-response recording for existing clients."""
import json
import threading
import time
import uuid
from pathlib import Path


def _recording_output_file(root: Path, idx, suffix: str) -> Path:
    """Build a direct output child without importing the selected code tree.

    The paired-run harness puts the baseline snapshot first on ``sys.path``.
    Keeping this tiny guard local prevents that snapshot from shadowing host
    safety helpers while retaining the same fail-closed path contract.
    """
    if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0:
        raise ValueError("idx must be a non-negative integer")
    base = Path(root).resolve()
    candidate = (base / f"{idx}{suffix}").resolve()
    if candidate.parent != base:
        raise ValueError("output path escapes its directory")
    return candidate


class RecordedClient:
    def __init__(self, client, directory: Path, idx, http_probe=None):
        self.client = client
        self.path = _recording_output_file(Path(directory), idx, ".calls.jsonl")
        self.lock = threading.Lock()
        self.http_probe = http_probe

    def for_problem(self, idx):
        return self

    def __getattr__(self, name):
        return getattr(self.client, name)

    def chat(self, messages, temperature=0.2, max_tokens=4096):
        if self.http_probe:
            # Reserve before delegating: a graph may issue many successful,
            # distinct chat calls, so checking only between problems is not a
            # budget boundary.
            self.http_probe.reserve_chat()
        started = time.monotonic()
        before = self.http_probe.count() if self.http_probe else None
        record = {"call_id": uuid.uuid4().hex, "messages": messages,
                  "temperature": temperature, "max_tokens": max_tokens}
        try:
            response = self.client.chat(messages=messages, temperature=temperature, max_tokens=max_tokens)
            record.update(response=str(response), status="success")
            return response
        except Exception as exc:
            record.update(status="transport_failed", error=str(exc))
            raise
        finally:
            tls = getattr(self.client, "_tls", None)
            record.update(elapsed_s=time.monotonic() - started,
                          finish_reason=getattr(tls, "finish_reason", None),
                          usage=getattr(tls, "usage", None),
                          http_attempts=(self.http_probe.count() - before if self.http_probe
                                         else getattr(tls, "http_attempts", None)))
            # Diagnostic disk failure must not discard an obtained mathematical answer.
            try:
                with self.lock:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                pass


class HttpBudgetExceeded(RuntimeError):
    pass


def install_http_probe(max_attempts=None):
    """Install two independent limits for a version-isolated batch run.

    ``reserve_chat`` is the primary admission gate.  The harness creates its
    transport with ``retry=1``, making one admitted chat correspond to at most
    one HTTP attempt.  The ``requests.post`` wrapper remains a fail-closed
    backstop for unwrapped/direct transport calls and counts observed attempts
    for the per-call audit record.
    """
    import requests
    tls = threading.local()
    original = requests.post
    lock = threading.Lock()
    transport_total = 0
    admitted_total = 0

    def _reserve_chat():
        nonlocal admitted_total
        with lock:
            if max_attempts is not None and admitted_total >= max_attempts:
                raise HttpBudgetExceeded("Local HTTP request budget exhausted; no chat was admitted")
            admitted_total += 1

    def post(*args, **kwargs):
        nonlocal transport_total
        with lock:
            if max_attempts is not None and transport_total >= max_attempts:
                raise HttpBudgetExceeded("Local HTTP request budget exhausted; no request was sent")
            transport_total += 1
        tls.count = getattr(tls, "count", 0) + 1
        return original(*args, **kwargs)
    requests.post = post
    class Probe:
        reserve_chat = staticmethod(_reserve_chat)

        def count(self):
            return getattr(tls, "count", 0)

        def admitted(self):
            with lock:
                return admitted_total

        def transport_attempts(self):
            with lock:
                return transport_total

        def exhausted(self):
            with lock:
                return (max_attempts is not None
                        and (admitted_total >= max_attempts or transport_total >= max_attempts))
    return Probe()
