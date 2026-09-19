"""_relay driven end to end against a scripted upstream. Still no network.

Pins the request line format, the attempt counting, the counters, and the
learn-and-retry loop as the client sees them - status line, chunked body, log.
"""

import json
import re

from conftest import make_handler

LINE = re.compile(r"r\d+ POST /v1/messages (\d+) (\d+)ms attempts=(\d+) \[(.*?)\](.*)")


def ok(payload):
    return (200, json.dumps(payload).encode(), "application/json")


TEXT_REPLY = {"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn"}


def test_relay_rewrites_and_logs_one_line(relay, clean_state, tmp_path):
    raw = json.dumps({
        "max_tokens": 200, "messages": [],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
    }).encode()
    response, sent = relay(raw, [ok(TEXT_REPLY)])
    assert response.startswith(b"HTTP/1.1 200 ")
    assert b'"hi"' in response
    assert len(sent) == 1
    assert json.loads(sent[0])["max_tokens"] == clean_state.MIN_MAX_TOKENS
    assert "max_uses" not in json.loads(sent[0])["tools"][0]
    match = LINE.search((tmp_path / "proxy.log").read_text())
    assert match and match.groups()[:3] == ("200", match.group(2), "1")
    assert match.group(4) == "-max_uses max_tokens 200->4096"
    assert clean_state._counters["by_status"]["2xx"] == 1


def test_relay_learns_from_400_then_succeeds(relay, clean_state, tmp_path):
    raw = json.dumps({"max_tokens": 4096, "taught_field": 1, "messages": []}).encode()
    first = (400, b'{"error": {"message": "unknown parameter `taught_field`"}}',
             "application/json")
    response, sent = relay(raw, [first, ok(TEXT_REPLY)])
    assert response.startswith(b"HTTP/1.1 200 ")
    assert len(sent) == 2
    assert "taught_field" not in json.loads(sent[1])
    assert "taught_field" in clean_state._learned
    assert clean_state._counters["learned_hits_total"] == 1
    match = LINE.search((tmp_path / "proxy.log").read_text())
    assert match and match.group(3) == "2" and "-taught_field" in match.group(4)


def test_relay_reports_an_unrepairable_400(relay, clean_state, tmp_path):
    raw = json.dumps({"max_tokens": 4096, "messages": []}).encode()
    response, _ = relay(raw, [(400, b"mystery failure, no backticks", "text/plain")])
    assert response.startswith(b"HTTP/1.1 400 ")
    assert b"mystery failure" in response
    assert clean_state._counters["by_status"]["400"] == 1
    assert "unnamed" in (tmp_path / "proxy.log").read_text()


def test_relay_injects_sources_on_json(relay, clean_state):
    raw = json.dumps({
        "max_tokens": 4096, "messages": [],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }).encode()
    reply = {"content": [{"type": "server_tool_use", "id": "srv_1",
                           "input": {"type": "open_page", "url": "https://a.example/x"}}]}
    response, _ = relay(raw, [ok(reply)])
    assert b"web_search_tool_result" in response
    assert b"https://a.example/x" in response


def test_relay_passes_sse_through_and_injects_before_stop(relay, clean_state):
    raw = json.dumps({
        "max_tokens": 4096, "messages": [], "stream": True,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }).encode()
    start = {"type": "content_block_start", "index": 0, "content_block": {
        "type": "server_tool_use", "id": "srv_9",
        "input": {"type": "open_page", "url": "https://b.example/y"}}}
    stream = (
        f"event: content_block_start\ndata: {json.dumps(start)}\n\n"
        'event: message_stop\ndata: {"type": "message_stop"}\n\n'
    ).encode()
    response, _ = relay(raw, [(200, stream, "text/event-stream")])
    assert b"web_search_tool_result" in response
    assert response.index(b"web_search_tool_result") < response.index(b"message_stop")


def test_relay_counts_and_ignores_the_hello_ping(relay, clean_state, tmp_path):
    response, _ = relay(b"", [(404, b"nope", "text/plain")],
                        path="/api/hello", command="GET")
    assert response.startswith(b"HTTP/1.1 404 ")
    assert clean_state._counters["requests_total"] == 0
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
    assert clean_state._counters["by_status"]["5xx"] == 1
    assert "upstream-error" in (tmp_path / "proxy.log").read_text()


def test_count_buckets_statuses(clean_state):
    for status, bucket in [(200, "2xx"), (400, "400"), (429, "429"),
                            (503, "5xx"), (401, "other"), (404, "other")]:
        clean_state._count(status)
    assert clean_state._counters["requests_total"] == 6
    assert clean_state._counters["by_status"] == {
        "2xx": 1, "400": 1, "429": 1, "5xx": 1, "other": 2}


def test_request_line_format(clean_state):
    line = clean_state._request_line
    assert (line("r7", "POST", "/v1/messages", 200, 812, 2, "-max_uses")
            == "r7 POST /v1/messages 200 812ms attempts=2 [-max_uses]")
    assert line("r7", "POST", "/v1/x", 502, 3, 1, "-", "boom").endswith("[-] boom")


def test_remember_counts_new_fields_only(clean_state):
    clean_state.remember("fresh_field")
    clean_state.remember("fresh_field")
    assert clean_state._counters["learned_hits_total"] == 1


def test_health_reports_counters_and_stays_silent(relay, clean_state, tmp_path):
    clean_state._count(200)
    response, _ = relay(b"", [(200, b"", "application/json")],
                        path="/__health", command="GET")
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
    limited = (429, b'{"error": {"message": "rate_limit exceeded"}}',
               "application/json", {"Retry-After": "2"})
    response, sent = relay(raw, [limited, ok(TEXT_REPLY)])
    assert response.startswith(b"HTTP/1.1 200 ")
    assert len(sent) == 2 and sent[0] == sent[1]  # resent unchanged
    assert slept == [2.0]
    assert clean_state._counters["transient_retries_total"] == 1
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
    assert clean_state._counters["transient_retries_total"] == 3
    assert clean_state._counters["by_status"]["429"] == 1
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
    too_big = (400, b'{"error": {"message": "prompt is too long: 2000000 tokens"}}',
               "application/json")
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
    assert clean_state._counters["by_status"]["other"] == 1


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
