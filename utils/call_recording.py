"""Optional local per-problem visible-response recording for existing clients."""
import json
import threading
import time
import uuid
from pathlib import Path


class RecordedClient:
    def __init__(self, client, directory: Path, idx, http_probe=None):
        self.client = client
        self.path = Path(directory) / f"{idx}.calls.jsonl"
        self.lock = threading.Lock()
        self.http_probe = http_probe

    def for_problem(self, idx):
        return self

    def __getattr__(self, name):
        return getattr(self.client, name)

    def chat(self, messages, temperature=0.2, max_tokens=4096):
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
    """Local harness only: observe actual requests.post attempts in either version."""
    import requests
    tls = threading.local()
    original = requests.post
    lock = threading.Lock()
    total = 0
    def post(*args, **kwargs):
        nonlocal total
        with lock:
            if max_attempts is not None and total >= max_attempts:
                raise HttpBudgetExceeded("Local HTTP request budget exhausted; no request was sent")
            total += 1
        tls.count = getattr(tls, "count", 0) + 1
        return original(*args, **kwargs)
    requests.post = post
    class Probe:
        def count(self):
            return getattr(tls, "count", 0)
        def exhausted(self):
            with lock:
                return max_attempts is not None and total >= max_attempts
    return Probe()
