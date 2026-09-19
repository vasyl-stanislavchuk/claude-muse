"""_relay driven end to end against a scripted upstream. Still no network.

Pins the request line format, the attempt counting, the counters, and the
learn-and-retry loop as the client sees them - status line, chunked body, log.
"""

import json
import re

from conftest import body, make_handler

LINE = re.compile(r"r\d+ POST /v1/messages (\d+) (\d+)ms attempts=(\d+) \[(.*?)\](.*)")


def ok(payload):
    return (200, json.dumps(payload).encode(), "application/json")


TEXT_REPLY = {"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn"}


def test_relay_rewrites_and_logs_one_line(relay, clean_state, tmp_path):
    raw = json.dumps(
        {
            "max_tokens": 200,
            "messages": [],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
        }
    ).encode()
    response, sent = relay(raw, [ok(TEXT_REPLY)])
    assert response.startswith(b"HTTP/1.1 200 ")
    assert b'"hi"' in response
    assert len(sent) == 1
    assert json.loads(sent[0])["max_tokens"] == clean_state.MIN_MAX_TOKENS
    assert "max_uses" not in json.loads(sent[0])["tools"][0]
    match = LINE.search((tmp_path / "proxy.log").read_text())
    assert match and match.groups()[:3] == ("200", match.group(2), "1")
    assert match.group(4) == "-max_uses max_tokens 200->4096"
    assert clean_state._counters.by_status["2xx"] == 1


def test_relay_learns_from_400_then_succeeds(relay, clean_state, tmp_path):
    raw = json.dumps({"max_tokens": 4096, "taught_field": 1, "messages": []}).encode()
    first = (400, b'{"error": {"message": "unknown parameter `taught_field`"}}', "application/json")
    response, sent = relay(raw, [first, ok(TEXT_REPLY)])
    assert response.startswith(b"HTTP/1.1 200 ")
    assert len(sent) == 2
    assert "taught_field" not in json.loads(sent[1])
    assert "taught_field" in clean_state._learned
    assert clean_state._counters.learned_hits_total == 1
    match = LINE.search((tmp_path / "proxy.log").read_text())
    assert match and match.group(3) == "2" and "-taught_field" in match.group(4)


def test_relay_reports_an_unrepairable_400(relay, clean_state, tmp_path):
    raw = json.dumps({"max_tokens": 4096, "messages": []}).encode()
    response, _ = relay(raw, [(400, b"mystery failure, no backticks", "text/plain")])
    assert response.startswith(b"HTTP/1.1 400 ")
    assert b"mystery failure" in response
    assert clean_state._counters.by_status["400"] == 1
    assert "unnamed" in (tmp_path / "proxy.log").read_text()


def test_relay_injects_sources_on_json(relay, clean_state):
    raw = json.dumps(
        {
            "max_tokens": 4096,
            "messages": [],
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        }
    ).encode()
    reply = {
        "content": [
            {
                "type": "server_tool_use",
                "id": "srv_1",
                "input": {"type": "open_page", "url": "https://a.example/x"},
            }
        ]
    }
    response, _ = relay(raw, [ok(reply)])
    assert b"web_search_tool_result" in response
    assert b"https://a.example/x" in response


def test_relay_passes_sse_through_and_injects_before_stop(relay, clean_state):
    raw = json.dumps(
        {
            "max_tokens": 4096,
            "messages": [],
            "stream": True,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        }
    ).encode()
    start = {
        "type": "content_block_start",
        "index": 0,
        "content_block": {
            "type": "server_tool_use",
            "id": "srv_9",
            "input": {"type": "open_page", "url": "https://b.example/y"},
        },
    }
    stream = (
        f"event: content_block_start\ndata: {json.dumps(start)}\n\n"
        'event: message_stop\ndata: {"type": "message_stop"}\n\n'
    ).encode()
    response, _ = relay(raw, [(200, stream, "text/event-stream")])
    assert b"web_search_tool_result" in response
    assert response.index(b"web_search_tool_result") < response.index(b"message_stop")


def test_relay_counts_and_ignores_the_hello_ping(relay, clean_state, tmp_path):
    response, _ = relay(b"", [(404, b"nope", "text/plain")], path="/api/hello", command="GET")
    assert response.startswith(b"HTTP/1.1 404 ")
    assert clean_state._counters.requests_total == 0
    assert not (tmp_path / "proxy.log").exists()


def test_relay_counts_an_upstream_error(relay, clean_state, tmp_path, monkeypatch):
    import http.client

    slept = []
    monkeypatch.setattr(clean_state, "_sleep", slept.append)

    def failing(body):
        raise http.client.RemoteDisconnected("closed")

    handler = make_handler(clean_state, "POST", "/v1/messages", b"{}")
    handler._upstream = failing
    handler._relay()
    assert handler.wfile.getvalue().startswith(b"HTTP/1.1 502 ")
    assert len(slept) == 3  # waited first, gave up second
    assert clean_state._cooldown_until is not None
    assert clean_state._counters.by_status["5xx"] == 1
    assert "upstream-error" in (tmp_path / "proxy.log").read_text()


def test_health_reports_counters_and_stays_silent(relay, clean_state, tmp_path):
    clean_state._count(200)
    response, _ = relay(b"", [(200, b"", "application/json")], path="/__health", command="GET")
    head, _, body = response.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 ")
    health = json.loads(body)
    assert health["requests"] == 1 and health["by_status"]["2xx"] == 1
    assert health["transient_retries"] == 0 and health["learned_hits"] == 0
    assert health["cooldown_until"] is None and health["uptime_s"] >= 0
    assert health["learned"] == sorted(clean_state._learned)
    assert not (tmp_path / "proxy.log").exists()


# Transient taxonomy: wait and resend, stop on request-scoped errors, cool down


def test_relay_waits_on_429_then_succeeds(relay, clean_state, tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(clean_state, "_sleep", slept.append)
    raw = json.dumps({"max_tokens": 4096, "messages": []}).encode()
    limited = (
        429,
        b'{"error": {"message": "rate_limit exceeded"}}',
        "application/json",
        {"Retry-After": "2"},
    )
    response, sent = relay(raw, [limited, ok(TEXT_REPLY)])
    assert response.startswith(b"HTTP/1.1 200 ")
    assert len(sent) == 2 and sent[0] == sent[1]  # resent unchanged
    assert slept == [2.0]
    assert clean_state._counters.transient_retries_total == 1
    assert clean_state._cooldown_until is None
    match = LINE.search((tmp_path / "proxy.log").read_text())
    assert match and match.group(3) == "2"
    assert match.group(4) == "- transient 429:2s"


def test_relay_cools_down_after_repeated_429s(relay, clean_state, tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(clean_state, "_sleep", slept.append)
    raw = json.dumps({"max_tokens": 4096, "messages": []}).encode()
    response, sent = relay(raw, [(429, b"slow down", "text/plain")])
    assert response.startswith(b"HTTP/1.1 429 ")
    assert b"slow down" in response  # the upstream's own bytes, served back
    assert len(sent) == 4 and len(slept) == 3
    assert clean_state._cooldown_until is not None
    assert clean_state._counters.transient_retries_total == 3
    assert clean_state._counters.by_status["429"] == 1
    log = (tmp_path / "proxy.log").read_text()
    assert "attempts=4" in log and "transient 429:" in log


def test_relay_fails_fast_while_cooling_down(relay, clean_state, tmp_path):
    raw = json.dumps({"max_tokens": 4096, "messages": []}).encode()
    clean_state._enter_cooldown()
    response, sent = relay(raw, [ok(TEXT_REPLY)])
    assert response.startswith(b"HTTP/1.1 503 ")
    assert sent == []  # never touched upstream
    assert b"cooling down" in response
    assert "[cooldown]" in (tmp_path / "proxy.log").read_text()


def test_relay_stops_on_context_too_long(relay, clean_state, tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(clean_state, "_sleep", slept.append)
    raw = json.dumps({"max_tokens": 4096, "messages": []}).encode()
    too_big = (
        400,
        b'{"error": {"message": "prompt is too long: 2000000 tokens"}}',
        "application/json",
    )
    response, sent = relay(raw, [too_big])
    assert response.startswith(b"HTTP/1.1 400 ")
    assert b"too long" in response and b"api_error" not in response
    assert len(sent) == 1 and slept == []
    assert clean_state._cooldown_until is None
    assert "compact or trim" in (tmp_path / "proxy.log").read_text()


def test_relay_passes_auth_failures_straight_through(relay, clean_state, tmp_path):
    raw = json.dumps({"max_tokens": 4096, "messages": []}).encode()
    denied = (401, b'{"error": {"message": "invalid api key"}}', "application/json")
    response, sent = relay(raw, [denied])
    assert response.startswith(b"HTTP/1.1 401 ")
    assert len(sent) == 1
    assert clean_state._cooldown_until is None
    assert clean_state._counters.by_status["other"] == 1


def test_relay_retries_dropped_connections(relay, clean_state, tmp_path, monkeypatch):
    import http.client

    slept = []
    monkeypatch.setattr(clean_state, "_sleep", slept.append)
    raw = json.dumps({"max_tokens": 4096, "messages": []}).encode()
    gone = http.client.RemoteDisconnected("closed")
    response, sent = relay(raw, [gone, ok(TEXT_REPLY)])
    assert response.startswith(b"HTTP/1.1 200 ")
    assert len(sent) == 1 and len(slept) == 1
    assert "transient conn:" in (tmp_path / "proxy.log").read_text()


def test_relay_applies_hand_edited_rules_without_restart(relay, clean_state, tmp_path):
    import yaml

    rules_file = tmp_path / "rewrite-rules.yaml"
    rules_file.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "rules": [
                    {"path": "tier", "op": "set_value", "value": "priority"},
                ],
                "models": {},
            }
        )
    )
    raw = json.dumps({"max_tokens": 4096, "tier": "standard", "messages": []}).encode()
    response, sent = relay(raw, [ok(TEXT_REPLY)])
    assert response.startswith(b"HTTP/1.1 200 ")
    assert json.loads(sent[0])["tier"] == "priority"
    assert "reloaded" in (tmp_path / "proxy.log").read_text()


# SSE bootstrap: hold headers for the first event-group, retry embedded overloads


def sse_event(payload):
    return f"event: message\ndata: {json.dumps(payload)}\n\n".encode()


OVERLOADED_STREAM = sse_event(
    {
        "type": "error",
        "error": {"type": "overloaded_error", "message": "Overloaded, try again shortly"},
    }
)


def test_sse_prefix_error_detection(clean_state):
    detect = clean_state._sse_prefix_error
    assert detect(OVERLOADED_STREAM)[0] is True
    assert "overloaded" in detect(OVERLOADED_STREAM)[1].lower()
    assert detect(sse_event({"type": "message_start"})) == (False, "")
    assert detect(b"event: done\ndata: [DONE]\n\n") == (False, "")
    assert detect(b"data: [1, 2]\n\n") == (False, "")
    assert detect(b": ping\n\n") == (False, "")


def test_read_sse_prefix_caps_bytes(clean_state):
    from conftest import FakeConn, FakeUpstream

    blob = b"x" * 40000
    prefix = clean_state._read_sse_prefix(FakeConn(), FakeUpstream(200, blob, "text/event-stream"))
    assert prefix == blob[:32768]
    short = clean_state._read_sse_prefix(
        FakeConn(), FakeUpstream(200, b"data: 1\n\ntrailing", "text/event-stream")
    )
    assert short == b"data: 1\n\ntrailing"


def test_relay_retries_embedded_overload(relay, clean_state, tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(clean_state, "_sleep", slept.append)
    raw = json.dumps({"max_tokens": 4096, "messages": [], "stream": True}).encode()
    good = (200, sse_event({"type": "message_stop"}), "text/event-stream")
    response, sent = relay(raw, [(200, OVERLOADED_STREAM, "text/event-stream"), good])
    assert response.startswith(b"HTTP/1.1 200 ")
    assert b"message_stop" in response and b"overloaded" not in response.lower()
    assert len(sent) == 2 and len(slept) == 1
    assert clean_state._counters.transient_retries_total == 1
    assert "transient sse:" in (tmp_path / "proxy.log").read_text()


def test_relay_cools_down_on_endless_embedded_overload(relay, clean_state, tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(clean_state, "_sleep", slept.append)
    raw = json.dumps({"max_tokens": 4096, "messages": [], "stream": True}).encode()
    response, sent = relay(raw, [(200, OVERLOADED_STREAM, "text/event-stream")])
    assert response.startswith(b"HTTP/1.1 200 ")
    assert b"overloaded" in response.lower()  # passed through after giving up
    assert len(sent) == 4 and len(slept) == 3
    assert clean_state._cooldown_until is not None


def test_relay_passes_stop_errors_through(relay, clean_state, tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(clean_state, "_sleep", slept.append)
    raw = json.dumps({"max_tokens": 4096, "messages": [], "stream": True}).encode()
    too_big = sse_event({"type": "error", "error": {"message": "prompt is too long"}})
    response, sent = relay(raw, [(200, too_big, "text/event-stream")])
    assert response.startswith(b"HTTP/1.1 200 ")
    assert b"too long" in response
    assert len(sent) == 1 and slept == []
    assert clean_state._cooldown_until is None
    assert "sse-error" in (tmp_path / "proxy.log").read_text()


def test_pump_tolerates_odd_frames(relay, clean_state, tmp_path):
    raw = json.dumps(
        {
            "max_tokens": 4096,
            "messages": [],
            "stream": True,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        }
    ).encode()
    stream = (
        b": ping\n\ndata: [1, 2]\n\nevent: done\ndata: [DONE]\n\n"
        b'event: message_stop\ndata: {"type": "message_stop"}\n\n'
    )
    response, _ = relay(raw, [(200, stream, "text/event-stream")])
    assert response.startswith(b"HTTP/1.1 200 ")
    assert b"ping" in response and b"[DONE]" in response  # passed through intact


# Timeouts: fast connect, patient reads, a wall clock against runaways


def test_upstream_splits_timeouts(clean_state, monkeypatch):
    created = {}

    class FakeSock:
        def __init__(self):
            self.timeouts = []

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

    class FakeHTTPS:
        def __init__(self, host, timeout=None):
            created["timeout"] = timeout
            self.sock = FakeSock()
            created["sock"] = self.sock

        def connect(self):
            created["connected"] = True

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return "resp"

    monkeypatch.setattr(clean_state.http.client, "HTTPSConnection", FakeHTTPS)
    handler = make_handler(clean_state, "POST", "/v1/messages", b"{}")
    conn, upstream = handler._upstream(b"{}")
    assert upstream == "resp"
    assert created["timeout"] == 10
    assert created["connected"] is True
    assert created["sock"].timeouts == [600]


def test_relay_cuts_passthrough_at_the_deadline(relay, clean_state, tmp_path, monkeypatch):
    monkeypatch.setattr(clean_state, "STREAM_TIMEOUT", -1.0)
    raw = json.dumps({"max_tokens": 4096, "messages": []}).encode()
    response, _ = relay(raw, [ok(TEXT_REPLY)])
    assert response.startswith(b"HTTP/1.1 200 ")
    assert b'"hi"' not in response  # nothing streamed past the deadline
    assert response.count(b"0\r\n\r\n") == 1  # framing still valid
    assert "stream-timeout" in (tmp_path / "proxy.log").read_text()


def test_relay_cuts_sse_pump_at_the_deadline(relay, clean_state, tmp_path, monkeypatch):
    monkeypatch.setattr(clean_state, "STREAM_TIMEOUT", -1.0)
    raw = json.dumps(
        {
            "max_tokens": 4096,
            "messages": [],
            "stream": True,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        }
    ).encode()
    stream = sse_event({"type": "message_start"}) + sse_event({"type": "message_stop"})
    response, _ = relay(raw, [(200, stream, "text/event-stream")])
    assert response.startswith(b"HTTP/1.1 200 ")
    assert "stream-timeout" in (tmp_path / "proxy.log").read_text()


def test_pump_json_skips_empty_bodies(relay, clean_state):
    raw = json.dumps(
        {
            "max_tokens": 4096,
            "messages": [],
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        }
    ).encode()
    response, _ = relay(raw, [(200, b"", "application/json")])
    assert response.startswith(b"HTTP/1.1 200 ")
    assert response.count(b"0\r\n\r\n") == 1  # one terminator, not two


# Usage observation: model, tokens, and substitution warnings


def test_fold_usage_holder_replaces(clean_state):
    fold = clean_state._fold_usage_holder
    usage = {}
    fold(usage, {"model": "a", "usage": {"input_tokens": 10, "output_tokens": 3}})
    fold(usage, {"usage": {"output_tokens": 9}})
    fold(usage, "not a dict")
    fold(usage, {"usage": {"input_tokens": "lots"}})
    assert usage == {"model": "a", "input_tokens": 10, "output_tokens": 9}


def test_note_usage_totals_samples_and_substitutions(clean_state, tmp_path):
    note = clean_state._note_usage
    note("m", {"model": "m", "input_tokens": 100, "output_tokens": 20}, 400, False)
    note("m", {"model": "other", "input_tokens": 50}, 200, False)
    note("m", {"model": "m", "input_tokens": 5000}, 400, True)  # tools: totals only
    note("m", {}, 100, False)
    note(None, {"model": "m", "input_tokens": 10}, 40, False)
    assert clean_state._usage_totals == {"input_tokens": 5160, "output_tokens": 20}
    assert list(clean_state._ratio_samples) == [(400, 100), (200, 50), (40, 10)]
    log = (tmp_path / "proxy.log").read_text()
    assert "model-substitution requested=m served=other" in log
    assert log.count("model-substitution") == 1


def test_relay_skips_samples_for_tool_requests(relay, clean_state):
    raw = json.dumps(
        {
            "max_tokens": 4096,
            "messages": [],
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        }
    ).encode()
    reply = dict(TEXT_REPLY, usage={"input_tokens": 5000, "output_tokens": 100})
    relay(raw, [ok(reply)])
    assert clean_state._usage_totals["input_tokens"] == 5000
    assert list(clean_state._ratio_samples) == []


def test_relay_reports_json_usage_in_the_line(relay, clean_state, tmp_path):
    raw = json.dumps({"model": "muse-spark-1.3", "max_tokens": 4096, "messages": []}).encode()
    reply = dict(
        TEXT_REPLY, model="muse-spark-1.3", usage={"input_tokens": 100, "output_tokens": 20}
    )
    response, _ = relay(raw, [ok(reply)])
    assert response.startswith(b"HTTP/1.1 200 ")
    match = LINE.search((tmp_path / "proxy.log").read_text())
    assert (
        match and match.group(5).strip() == "in=100 out=20 stop=end_turn blocks=text:1 no-reasoning"
    )
    assert "model-substitution" not in (tmp_path / "proxy.log").read_text()
    assert clean_state._usage_totals == {"input_tokens": 100, "output_tokens": 20}
    assert list(clean_state._ratio_samples) == [(len(raw), 100)]


def test_relay_warns_on_model_substitution(relay, clean_state, tmp_path):
    raw = json.dumps({"model": "muse-spark-1.3", "max_tokens": 4096, "messages": []}).encode()
    reply = dict(TEXT_REPLY, model="something-else", usage={"input_tokens": 10})
    relay(raw, [ok(reply)])
    log = (tmp_path / "proxy.log").read_text()
    assert "model-substitution requested=muse-spark-1.3 served=something-else" in log


def test_relay_reads_sse_usage_without_sources(relay, clean_state, tmp_path):
    raw = json.dumps({"model": "m", "max_tokens": 4096, "messages": [], "stream": True}).encode()
    start = {
        "type": "message_start",
        "message": {"model": "m", "usage": {"input_tokens": 300, "output_tokens": 0}},
    }
    delta = {
        "type": "message_delta",
        "usage": {"output_tokens": 45},
        "delta": {"type": "text_delta"},
    }
    stream = sse_event(start) + sse_event(delta) + sse_event({"type": "message_stop"})
    response, _ = relay(raw, [(200, stream, "text/event-stream")])
    assert response.startswith(b"HTTP/1.1 200 ")
    assert b"web_search_tool_result" not in response  # observed, not injected
    match = LINE.search((tmp_path / "proxy.log").read_text())
    assert match and match.group(5).strip() == "in=300 out=45 blocks=none no-reasoning"


def test_relay_sse_usage_takes_the_last_delta(relay, clean_state, tmp_path):
    raw = json.dumps({"max_tokens": 4096, "messages": [], "stream": True}).encode()
    stream = (
        sse_event({"type": "message_delta", "usage": {"output_tokens": 10}})
        + sse_event({"type": "message_delta", "usage": {"output_tokens": 30}})
        + sse_event({"type": "message_stop"})
    )
    relay(raw, [(200, stream, "text/event-stream")])
    assert clean_state._usage_totals == {"input_tokens": 0, "output_tokens": 30}


# count_tokens: served locally from the calibrated ratio


def test_relay_serves_count_tokens_locally(relay, clean_state, tmp_path):
    raw = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode()
    response, sent = relay(raw, [ok(TEXT_REPLY)], path="/v1/messages/count_tokens")
    assert response.startswith(b"HTTP/1.1 200 ")
    assert sent == []  # upstream never asked
    body = json.loads(response.partition(b"\r\n\r\n")[2])
    assert body == {"input_tokens": -(-len(raw) // 4)}  # ceil(chars/4), no data yet
    log = (tmp_path / "proxy.log").read_text()
    assert "attempts=0 [estimated]" in log and "ratio=4.00 samples=0" in log
    assert clean_state._counters.by_status["2xx"] == 1


def test_relay_count_tokens_improves_with_use(relay, clean_state, tmp_path):
    teaching = json.dumps({"max_tokens": 4096, "messages": [{"role": "user"}]}).encode()
    reply = dict(TEXT_REPLY, usage={"input_tokens": 100, "output_tokens": 5})
    relay(teaching, [ok(reply)])
    ratio = len(teaching) / 100
    raw = json.dumps({"model": "m", "messages": []}).encode()
    response, _ = relay(raw, [ok(TEXT_REPLY)], path="/v1/messages/count_tokens")
    body = json.loads(response.partition(b"\r\n\r\n")[2])
    import math

    assert body == {"input_tokens": math.ceil(len(raw) / ratio)}
    assert f"ratio={ratio:.2f} samples=1" in (tmp_path / "proxy.log").read_text()


def test_relay_ignores_count_tokens_gets(relay, clean_state):
    response, sent = relay(
        b"", [(404, b"nope", "text/plain")], path="/v1/messages/count_tokens", command="GET"
    )
    assert response.startswith(b"HTTP/1.1 404 ")
    assert len(sent) == 1  # only POST is served locally


# --- outcome observability -------------------------------------------------


def test_relay_flags_an_empty_content_200(relay, clean_state, tmp_path):
    # The failure the max_tokens floor exists to prevent. Nothing proved it had
    # stopped happening, because nothing counted it.
    raw = json.dumps({"model": "muse-spark-1.3", "max_tokens": 4096, "messages": []}).encode()
    relay(
        raw,
        [
            ok(
                {
                    "content": [],
                    "stop_reason": "max_tokens",
                    "usage": {"input_tokens": 9, "output_tokens": 200},
                }
            )
        ],
    )
    match = LINE.search((tmp_path / "proxy.log").read_text())
    assert match and "stop=max_tokens blocks=none" in match.group(5)
    assert clean_state._counters.empty_content_200s == 1
    assert clean_state._counters.stop_reasons == {"max_tokens": 1}


def test_relay_tallies_mixed_blocks(relay, clean_state, tmp_path):
    raw = json.dumps({"model": "muse-spark-1.3", "max_tokens": 4096, "messages": []}).encode()
    relay(
        raw,
        [
            ok(
                {
                    "stop_reason": "tool_use",
                    "content": [
                        {"type": "text", "text": "a"},
                        {"type": "thinking", "thinking": "b"},
                        {"type": "tool_use", "id": "1", "name": "x", "input": {}},
                        {"type": "tool_use", "id": "2", "name": "y", "input": {}},
                    ],
                }
            )
        ],
    )
    match = LINE.search((tmp_path / "proxy.log").read_text())
    assert match and "stop=tool_use blocks=text:1,thinking:1,tool_use:2" in match.group(5)
    assert clean_state._counters.empty_content_200s == 0


def test_relay_does_not_tally_the_injected_sources_block(relay, clean_state, tmp_path):
    # The proxy's own web_search_tool_result must not read as model output.
    raw = json.dumps(
        {
            "model": "muse-spark-1.3",
            "max_tokens": 4096,
            "messages": [],
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        }
    ).encode()
    reply = {
        "stop_reason": "end_turn",
        "content": [
            {
                "type": "server_tool_use",
                "id": "s1",
                "name": "web_search",
                "input": {"type": "open_page", "url": "https://example.com/a"},
            },
            {"type": "text", "text": "done"},
        ],
    }
    response, _ = relay(raw, [ok(reply)])
    assert b"web_search_tool_result" in response
    match = LINE.search((tmp_path / "proxy.log").read_text())
    assert match and "blocks=server_tool_use:1,text:1" in match.group(5)


def test_relay_counts_the_no_reasoning_shape(relay, clean_state, tmp_path):
    bare = json.dumps({"model": "muse-spark-1.3", "max_tokens": 4096, "messages": []}).encode()
    relay(bare, [ok(TEXT_REPLY)])
    assert clean_state._counters.no_reasoning_requests_total == 1

    # A request that names a tier is not the classifier shape, whichever of the
    # two spellings it uses.
    with_effort = json.dumps(
        {
            "model": "muse-spark-1.3",
            "max_tokens": 4096,
            "messages": [],
            "output_config": {"effort": "max"},
        }
    ).encode()
    relay(with_effort, [ok(TEXT_REPLY)])
    thinking = json.dumps(
        {
            "model": "muse-spark-1.3",
            "max_tokens": 40000,
            "messages": [],
            "thinking": {"type": "adaptive"},
        }
    ).encode()
    relay(thinking, [ok(TEXT_REPLY)])
    assert clean_state._counters.no_reasoning_requests_total == 1


def test_relay_reports_thinking_tokens(relay, clean_state, tmp_path):
    raw = json.dumps(
        {
            "model": "muse-spark-1.3",
            "max_tokens": 40000,
            "messages": [],
            "thinking": {"type": "adaptive"},
        }
    ).encode()
    relay(
        raw,
        [
            ok(
                dict(
                    TEXT_REPLY,
                    usage={
                        "input_tokens": 47,
                        "output_tokens": 903,
                        "output_tokens_details": {"thinking_tokens": 706},
                    },
                )
            )
        ],
    )
    match = LINE.search((tmp_path / "proxy.log").read_text())
    assert match and "in=47 out=903 think=706" in match.group(5)


def test_relay_sse_tallies_blocks_and_stop_reason(relay, clean_state, tmp_path):
    raw = json.dumps({"model": "muse-spark-1.3", "max_tokens": 4096, "messages": []}).encode()
    stream = (
        sse_event(
            {
                "type": "message_start",
                "message": {
                    "type": "message",
                    "model": "muse-spark-1.3",
                    "stop_reason": None,
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            }
        )
        + sse_event(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            }
        )
        + sse_event(
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            }
        )
        + sse_event(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 12},
            }
        )
        + sse_event({"type": "message_stop"})
    )
    relay(raw, [(200, stream, "text/event-stream")])
    match = LINE.search((tmp_path / "proxy.log").read_text())
    # message_start carries stop_reason null; the delta's value must survive it.
    assert match and "stop=end_turn blocks=text:1,thinking:1" in match.group(5)
    assert clean_state._counters.empty_content_200s == 0


def test_health_reports_the_outcome_counters(relay, clean_state):
    raw = json.dumps({"model": "muse-spark-1.3", "max_tokens": 4096, "messages": []}).encode()
    relay(raw, [ok(TEXT_REPLY)])
    response, _ = relay(b"", [(200, b"", "application/json")], path="/__health", command="GET")
    health = json.loads(response.partition(b"\r\n\r\n")[2])
    assert health["stop_reasons"] == {"end_turn": 1}
    assert health["empty_content_200s"] == 0
    assert health["no_reasoning_requests"] == 1
    assert health["client_hangups"] == 0


# --- bootstrap peek must not poison the stream -----------------------------


def test_bootstrap_peek_never_arms_a_socket_timeout(proxy, clean_state, monkeypatch):
    """A slow first token must leave the response object readable.

    Arming a socket timeout for the peek poisons CPython's buffered reader: the
    read that times out is fine, but every later read raises `OSError: cannot
    read from timed out object`, so the stream that followed died and the
    request was lost. proxy.log showed it as `handler-error`.
    """
    import socket as _socket

    monkeypatch.setattr(proxy, "BOOTSTRAP_TIMEOUT", 0.05)
    left, right = _socket.socketpair()  # local, and never written to
    armed = []

    class WatchedSock:
        """Selectable, and records any attempt to arm a timeout."""

        def fileno(self):
            return left.fileno()

        def settimeout(self, t):
            armed.append(t)

    try:

        class Conn:
            sock = WatchedSock()

            def close(self):
                pass

        body = sse_event(
            {"type": "message_start", "message": {"type": "message", "usage": {"input_tokens": 1}}}
        )
        from conftest import FakeUpstream

        upstream = FakeUpstream(200, body, "text/event-stream")

        prefix = proxy._read_sse_prefix(Conn(), upstream)

        assert armed == [], f"peek armed a socket timeout: {armed}"
        # Nothing was readable inside the window, so the peek yields nothing and
        # leaves the whole stream for the pump to read normally.
        assert prefix == b""
        assert upstream.read(-1) == body
    finally:
        left.close()
        right.close()


def test_pump_sse_survives_an_upstream_that_dies_mid_stream(proxy, clean_state, tmp_path):
    """An upstream read failing after headers commit is logged, not raised."""

    class DyingUpstream:
        def __init__(self):
            self._first = True

        def read(self, n=-1):
            if self._first:
                self._first = False
                return sse_event(
                    {
                        "type": "message_start",
                        "message": {"type": "message", "usage": {"input_tokens": 7}},
                    }
                )
            raise OSError("cannot read from timed out object")

    handler = proxy.Handler.__new__(proxy.Handler)
    sent = []
    handler._chunk = sent.append

    usage = proxy.Handler._pump_sse(handler, DyingUpstream(), None)

    assert usage.get("input_tokens") == 7  # what did arrive is kept
    assert sent, "the bytes read before the cut are still served"
    log = (tmp_path / "proxy.log").read_text()
    assert "upstream-cut" in log


# wants_web_search


def test_wants_web_search(clean_state):
    yes = body({"tools": [{"type": "web_search_20250305"}]})
    no = body({"tools": [{"name": "bash"}]})
    assert clean_state.wants_web_search(yes) is True
    assert clean_state.wants_web_search(no) is False
    assert clean_state.wants_web_search(b"junk") is False


# title_from_url: honest labels only, never invented titles


def test_title_from_url(clean_state):
    title = clean_state.title_from_url
    assert title("https://example.com/docs/my-page.html") == "example.com — my page"
    assert title("https://www.example.com/") == "example.com"
    assert title("https://example.com/a_b-c") == "example.com — a b c"


# SearchSources: harvest open_page urls, build the block Claude Code parses


def test_search_sources_collects_and_dedupes(clean_state):
    sources = clean_state.SearchSources()
    sources.observe({"type": "text", "text": "hi"}, 0)
    sources.observe(
        {
            "type": "server_tool_use",
            "id": "srv_1",
            "input": {"type": "open_page", "url": "https://a.example/x"},
        },
        1,
    )
    sources.observe(
        {"type": "server_tool_use", "id": "srv_2", "input": {"type": "search", "query": "y"}}, 2
    )
    sources.observe(
        {
            "type": "server_tool_use",
            "id": "srv_3",
            "input": {"type": "open_page", "url": "https://a.example/x"},
        },
        3,
    )
    assert sources.urls == ["https://a.example/x"]
    assert sources.tool_use_id == "srv_1"
    assert sources.max_index == 3


def test_search_sources_block_shape_and_fallback_id(clean_state):
    sources = clean_state.SearchSources()
    assert sources.block() is None
    sources.observe(
        {"type": "server_tool_use", "input": {"type": "open_page", "url": "https://a.example/x"}}, 0
    )
    block = sources.block()
    assert block["type"] == "web_search_tool_result"
    assert block["tool_use_id"] == "ws_proxy_sources"
    assert block["content"][0]["url"] == "https://a.example/x"


# parse_retry_after, transient_backoff, _is_stop: the transient taxonomy


def test_parse_retry_after_seconds_and_absent(clean_state):
    parse = clean_state.parse_retry_after
    assert parse("120") == 120
    assert parse("  7 ") == 7
    assert parse(None) is None
    assert parse("") is None
    assert parse("soon") is None
    assert parse("12.5") is None


def test_parse_retry_after_http_date(clean_state):
    from datetime import datetime, timezone

    now = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
    parse = clean_state.parse_retry_after
    assert parse("Sat, 19 Sep 2026 12:00:10 GMT", now) == 10
    assert parse("Sat, 19 Sep 2026 11:59:00 GMT", now) == 0


def test_transient_backoff_honors_retry_after_with_cap(clean_state):
    assert clean_state.transient_backoff(0, 5) == 5.0
    assert clean_state.transient_backoff(0, 120) == 30.0


def test_transient_backoff_grows_exponentially(clean_state):
    def backoff(n):
        return clean_state.transient_backoff(n, None, rand_fn=lambda a, b: 1.0)

    assert [backoff(n) for n in range(4)] == [1.0, 2.0, 4.0, 8.0]
    assert backoff(10) == 30.0


def test_is_stop_and_hints(clean_state):
    is_stop, hint = clean_state._is_stop, clean_state._stop_hint
    assert is_stop(401, "") and is_stop(402, "whatever")
    assert not is_stop(429, "rate_limit exceeded, slow down")
    assert not is_stop(503, "overloaded")
    assert is_stop(400, "prompt is too long: 1200000 > 1000000")
    assert is_stop(429, "request reaches maximum context length")
    assert "compact" in hint(400, "context too long")
    assert "key" in hint(401, "")
    assert hint(503, "overloaded") == ""


def test_take_transient_wait_records_and_counts(clean_state, monkeypatch):
    slept = []
    monkeypatch.setattr(clean_state, "_sleep", slept.append)
    waits = []
    clean_state._take_transient_wait(waits, 1, "503")
    assert waits == [f"503:{slept[0]:g}s"]
    assert 1.0 <= slept[0] <= 2.0
    assert clean_state._counters.transient_retries_total == 1


def test_cooldown_window(clean_state):
    assert clean_state._in_cooldown(now=100.0) is False
    clean_state._enter_cooldown(now=100.0)
    assert clean_state._in_cooldown(now=159.9) is True
    assert clean_state._in_cooldown(now=160.0) is False
    assert clean_state._cooldown_until == 160.0


def test_deadline_hit(clean_state, tmp_path):
    import time

    assert clean_state._deadline_hit(None) is False
    assert clean_state._deadline_hit(time.monotonic() + 60) is False
    assert not (tmp_path / "proxy.log").exists()
    assert clean_state._deadline_hit(time.monotonic() - 1) is True
    assert "stream-timeout: closing after 3600s" in (tmp_path / "proxy.log").read_text()
