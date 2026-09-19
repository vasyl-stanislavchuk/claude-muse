"""Shared fixtures for the offline proxy suite.

The module under test is loaded the same way CLAUDE.md's recipe loads it:
straight from bin/proxy.py with importlib, no install step. Importing it reads
the real learned.json and shapes.json, which is safe (read-only, sane defaults
when they are missing), and every fixture below repoints the writable paths at
tmp_path so a test run never touches ~/.config.
"""

import importlib.util
import io
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("proxy_under_test", REPO / "bin" / "proxy.py")


@pytest.fixture(scope="session")
def proxy():
    module = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = module
    SPEC.loader.exec_module(module)
    return module


@pytest.fixture
def clean_state(proxy, tmp_path, monkeypatch):
    """Known learned/shapes sets and throwaway state files for one test."""
    monkeypatch.setattr(
        proxy, "_learned",
        {s: {"first_seen": None, "hits": 0} for s in proxy.SEEDED_DROPS},
    )
    monkeypatch.setattr(proxy, "_shapes", {})
    log = tmp_path / "proxy.log"
    monkeypatch.setattr(proxy, "LOG", str(log))
    monkeypatch.setattr(proxy, "LEARNED", str(tmp_path / "learned.json"))
    monkeypatch.setattr(proxy, "SHAPES", str(tmp_path / "shapes.json"))
    with proxy._counters_lock:
        proxy._counters["requests_total"] = 0
        for key in proxy._counters["by_status"]:
            proxy._counters["by_status"][key] = 0
        proxy._counters["transient_retries_total"] = 0
        proxy._counters["learned_hits_total"] = 0
    monkeypatch.setattr(proxy, "MAX_RETRY_INTERVAL", 30.0)
    monkeypatch.setattr(proxy, "TRANSIENT_COOLDOWN", 60.0)
    monkeypatch.setattr(proxy, "_cooldown_until", None)
    return proxy


class FakeUpstream:
    """Scripted stand-in for http.client.HTTPResponse: status, bytes, content type."""

    def __init__(self, status, body, content_type="application/json", extra=None):
        self.status = status
        self._buf = body
        self._headers = {"Content-Type": content_type}
        self._headers.update(extra or {})

    def getheaders(self):
        return list(self._headers.items())

    def getheader(self, name, default=None):
        for key, value in self._headers.items():
            if key.lower() == name.lower():
                return value
        return default

    def read(self, n=-1):
        if n is None or n < 0:
            out, self._buf = self._buf, b""
        else:
            out, self._buf = self._buf[:n], self._buf[n:]
        return out


class FakeConn:
    def close(self):
        pass


def make_handler(proxy, command, path, raw):
    """A Handler with just enough anatomy for _relay: no socket involved."""
    handler = proxy.Handler.__new__(proxy.Handler)
    handler.command = command
    handler.path = path
    handler.requestline = f"{command} {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    return handler


@pytest.fixture
def relay(proxy, clean_state):
    """Drive Handler._relay offline against a scripted upstream.

    Returns run(raw, script, path, command) -> (response_bytes, sent_bodies).
    Each script entry is (status, body_bytes, content_type) with an optional
    fourth element of extra headers, or an Exception to raise from _upstream.
    Attempt n consumes entry n, clamped to the last entry, so a one-entry
    script answers forever.
    """

    def run(raw: bytes, script, path="/v1/messages", command="POST"):
        sent = []
        calls = 0

        def fake_upstream(body):
            nonlocal calls
            entry = script[min(calls, len(script) - 1)]
            calls += 1
            if isinstance(entry, Exception):
                raise entry
            status, rbody, ctype = entry[0], entry[1], entry[2]
            extra = entry[3] if len(entry) > 3 else {}
            sent.append(body)
            return FakeConn(), FakeUpstream(status, rbody, ctype, extra)

        handler = make_handler(proxy, command, path, raw)
        handler._upstream = fake_upstream
        handler._relay()
        return handler.wfile.getvalue(), sent

    return run
