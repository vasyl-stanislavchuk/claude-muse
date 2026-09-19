"""Shared fixtures for the offline engine suite.

The engine is imported as a package straight from the repo root: no install
step, sys.path pointed at the checkout. Importing it reads the real
learned.json and shapes.json, which is safe (read-only, sane defaults when
they are missing), and every fixture below repoints the writable paths at
tmp_path so a test run never touches ~/.config.
"""

import copy
import http
import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import census as census_mod  # noqa: E402
from engine import policy as policy_mod  # noqa: E402
from engine import reasoning as reasoning_mod  # noqa: E402
from engine import relay as relay_mod  # noqa: E402
from engine import rewrite as rewrite_mod  # noqa: E402
from engine import server as server_mod  # noqa: E402
from engine import state as state_mod  # noqa: E402


class _Facade:
    """The engine as the tests see it: one object, canonical homes behind it.

    Reads and monkeypatch writes both forward to the module that owns each
    name, so the suite keeps working on attribute paths (clean_state._learned)
    while production code imports modules directly. A name missing here fails
    loud with AttributeError, which is how a test touching something new
    announces the new dependency.
    """

    def __init__(self):
        self.__dict__["_homes"] = {
            # state: process state, persistence, observability
            "LOG": state_mod,
            "LEARNED": state_mod,
            "CALIBRATION": state_mod,
            "_counters": state_mod,
            "_counters_lock": state_mod,
            "_request_line": state_mod,
            "_count": state_mod,
            "_usage_totals": state_mod,
            "_ratio_samples": state_mod,
            "_calibration": state_mod,
            "load_calibration": state_mod,
            "estimate_tokens": state_mod,
            "_learned": state_mod,
            "load_learned": state_mod,
            "remember": state_mod,
            "SEEDED_DROPS": state_mod,
            "LEARNED_MAX_FIELDS": state_mod,
            "_warned_owned": state_mod,
            "_atomic_write_json": state_mod,
            "Calibration": state_mod,
            "Counters": state_mod,
            "LearnedEntry": state_mod,
            # policy: repair rules and reasoning profiles
            "RULES": policy_mod,
            "BAKED_IN_RULES": policy_mod,
            "BAKED_IN_PROFILES": policy_mod,
            "MODEL_DEFAULTS": policy_mod,
            "_RULES": policy_mod,
            "_PROFILES": policy_mod,
            "_policy_mtimes": policy_mod,
            "load_rules": policy_mod,
            "load_profiles": policy_mod,
            "_maybe_reload_policy": policy_mod,
            "Rule": policy_mod,
            "Profile": policy_mod,
            # rewrite: the static rule engine
            "rewrite": rewrite_mod,
            "apply_rule": rewrite_mod,
            "offending_fields": rewrite_mod,
            "drop_field": rewrite_mod,
            "_warned_ops": rewrite_mod,
            # reasoning: the thinking/max_tokens pipeline
            "normalize_reasoning": reasoning_mod,
            "_profile_for": reasoning_mod,
            "MIN_MAX_TOKENS": reasoning_mod,
            "_warned_models": reasoning_mod,
            # relay: upstream traffic, transient policy, stream helpers
            "_sleep": relay_mod,
            "MAX_RETRY_INTERVAL": relay_mod,
            "TRANSIENT_COOLDOWN": relay_mod,
            "STREAM_TIMEOUT": relay_mod,
            "BOOTSTRAP_TIMEOUT": relay_mod,
            "_cooldown_until": relay_mod,
            "transient_backoff": relay_mod,
            "parse_retry_after": relay_mod,
            "_take_transient_wait": relay_mod,
            "_is_stop": relay_mod,
            "_stop_hint": relay_mod,
            "_read_sse_prefix": relay_mod,
            "_sse_prefix_error": relay_mod,
            "_deadline_hit": relay_mod,
            "_fold_usage_holder": relay_mod,
            "_note_usage": relay_mod,
            "_in_cooldown": relay_mod,
            "_enter_cooldown": relay_mod,
            "wants_web_search": relay_mod,
            "title_from_url": relay_mod,
            "SearchSources": relay_mod,
            # census: request shape observations
            "SHAPES": census_mod,
            "_shapes": census_mod,
            "SHAPES_MAX": census_mod,
            "census": census_mod,
            "signature": census_mod,
            # server: the HTTP layer
            "Handler": server_mod,
        }

    def __getattr__(self, name):
        if name == "http":  # stdlib, reached through the module as before
            return http
        try:
            return getattr(self._homes[name], name)
        except KeyError:
            raise AttributeError(f"engine facade has no attribute {name!r}") from None

    def __setattr__(self, name, value):
        try:
            setattr(self._homes[name], name, value)
        except KeyError:
            raise AttributeError(f"engine facade has no attribute {name!r}") from None


def body(payload):
    return json.dumps(payload).encode()


@pytest.fixture(scope="session")
def proxy():
    return _Facade()


@pytest.fixture
def clean_state(proxy, tmp_path, monkeypatch):
    """Known learned/shapes sets and throwaway state files for one test."""
    monkeypatch.setattr(
        proxy,
        "_learned",
        {s: proxy.LearnedEntry() for s in proxy.SEEDED_DROPS},
    )
    monkeypatch.setattr(proxy, "_shapes", {})
    log = tmp_path / "proxy.log"
    monkeypatch.setattr(proxy, "LOG", str(log))
    monkeypatch.setattr(proxy, "LEARNED", str(tmp_path / "learned.json"))
    monkeypatch.setattr(proxy, "SHAPES", str(tmp_path / "shapes.json"))
    monkeypatch.setattr(proxy, "RULES", str(tmp_path / "rewrite-rules.yaml"))
    monkeypatch.setattr(proxy, "CALIBRATION", str(tmp_path / "calibration.json"))
    monkeypatch.setattr(proxy, "_calibration", proxy.Calibration())
    monkeypatch.setattr(proxy, "_policy_mtimes", {})
    with proxy._counters_lock:
        proxy._counters.requests_total = 0
        for key in proxy._counters.by_status:
            proxy._counters.by_status[key] = 0
        proxy._counters.transient_retries_total = 0
        proxy._counters.learned_hits_total = 0
        proxy._counters.client_hangups_total = 0
        proxy._counters.empty_content_200s = 0
        proxy._counters.no_reasoning_requests_total = 0
        proxy._counters.stop_reasons.clear()
        proxy._usage_totals["input_tokens"] = 0
        proxy._usage_totals["output_tokens"] = 0
        proxy._ratio_samples.clear()
    monkeypatch.setattr(proxy, "MAX_RETRY_INTERVAL", 30.0)
    monkeypatch.setattr(proxy, "TRANSIENT_COOLDOWN", 60.0)
    monkeypatch.setattr(proxy, "STREAM_TIMEOUT", 3600.0)
    monkeypatch.setattr(proxy, "_cooldown_until", None)
    monkeypatch.setattr(proxy, "_RULES", copy.deepcopy(proxy.BAKED_IN_RULES))
    monkeypatch.setattr(proxy, "_PROFILES", copy.deepcopy(proxy.BAKED_IN_PROFILES))
    monkeypatch.setattr(proxy, "_warned_ops", set())
    monkeypatch.setattr(proxy, "_warned_models", set())
    monkeypatch.setattr(proxy, "_warned_owned", set())
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
    sock = None  # no real socket, so the bootstrap skips its timeout dance

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
