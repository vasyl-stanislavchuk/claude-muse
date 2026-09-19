"""Offline vectors for the proxy's pure functions. No network, no keychain.

Each test pins behavior the paid probe cannot check cheaply: the exact repair
rules, the learn-from-400 extraction, and the census honesty guarantees. A new
repair lands here first, then earns a probe row per CLAUDE.md.
"""

import json


def body(payload):
    return json.dumps(payload).encode()


# offending_fields: what the endpoint names in backticks, minus tool_choice


def test_offending_fields_collects_every_name(clean_state):
    detail = "`stop_sequences` is not supported, unknown parameter `safeguards`"
    assert clean_state.offending_fields(detail) == ["stop_sequences", "safeguards"]


def test_offending_fields_skips_tool_choice(clean_state):
    assert clean_state.offending_fields("named `tool_choice` is not supported") == []


def test_offending_fields_dedupes_and_ignores_plain_text(clean_state):
    detail = "`max_uses` is not supported on `max_uses`, see docs"
    assert clean_state.offending_fields(detail) == ["max_uses"]
    assert clean_state.offending_fields("overloaded, try again later") == []


# drop_field: top level plus every tool in the list


def test_drop_field_top_level_and_tools(clean_state):
    payload = {"safeguards": True, "tools": [{"name": "w", "safeguards": 1}, {"name": "x"}]}
    assert clean_state.drop_field(payload, "safeguards") is True
    assert payload == {"tools": [{"name": "w"}, {"name": "x"}]}


def test_drop_field_reports_absence(clean_state):
    assert clean_state.drop_field({"tools": None}, "safeguards") is False


# rewrite: web search fields


def test_rewrite_strips_web_search_limits(clean_state):
    payload = {
        "max_tokens": 4096,
        "tools": [{
            "type": "web_search_20250305", "name": "web_search",
            "max_uses": 8, "allowed_domains": ["a"], "blocked_domains": ["b"],
        }],
    }
    out, notes = clean_state.rewrite(body(payload))
    tool = json.loads(out)["tools"][0]
    assert "max_uses" not in tool and "allowed_domains" not in tool
    assert "blocked_domains" not in tool and tool["name"] == "web_search"
    assert sorted(notes) == ["-allowed_domains", "-blocked_domains", "-max_uses"]


def test_rewrite_leaves_other_tools_alone(clean_state):
    payload = {"max_tokens": 4096, "tools": [{"name": "bash", "max_uses": 8}]}
    raw = body(payload)
    out, notes = clean_state.rewrite(raw)
    assert out is raw and notes == []


# rewrite: learned fields


def test_rewrite_drops_learned_fields(clean_state):
    clean_state._learned["stop_sequences"] = {"first_seen": None, "hits": 0}
    payload = {"max_tokens": 4096, "stop_sequences": ["x"], "messages": []}
    out, notes = clean_state.rewrite(body(payload))
    assert "stop_sequences" not in json.loads(out)
    assert notes == ["-stop_sequences"]


# rewrite: tool_choice


def test_rewrite_named_tool_choice_becomes_auto(clean_state):
    payload = {"max_tokens": 4096, "tool_choice": {"type": "tool", "name": "web_search"}}
    out, notes = clean_state.rewrite(body(payload))
    assert json.loads(out)["tool_choice"] == {"type": "auto"}
    assert notes == ["tool_choice tool->auto"]


def test_rewrite_any_and_none_pass_through(clean_state):
    for choice in ({"type": "any"}, {"type": "none"}):
        raw = body({"max_tokens": 4096, "tool_choice": choice})
        out, notes = clean_state.rewrite(raw)
        assert out is raw and notes == []


def test_rewrite_drop_tool_choice_removes_the_key(clean_state):
    payload = {"max_tokens": 4096, "tool_choice": {"type": "none"}}
    out, notes = clean_state.rewrite(body(payload), drop_tool_choice=True)
    assert "tool_choice" not in json.loads(out)
    assert notes == ["-tool_choice"]


# rewrite: thinking, max_tokens, budget


def test_rewrite_omits_disabled_thinking(clean_state):
    payload = {"max_tokens": 4096, "thinking": {"type": "disabled"}}
    out, notes = clean_state.rewrite(body(payload))
    assert "thinking" not in json.loads(out)
    assert notes == ["thinking disabled->omitted"]


def test_rewrite_floors_small_max_tokens(clean_state):
    out, notes = clean_state.rewrite(body({"max_tokens": 200, "messages": []}))
    assert json.loads(out)["max_tokens"] == clean_state.MIN_MAX_TOKENS
    assert notes == [f"max_tokens 200->{clean_state.MIN_MAX_TOKENS}"]


def test_rewrite_leaves_roomy_max_tokens_alone(clean_state):
    raw = body({"max_tokens": 8192})
    out, notes = clean_state.rewrite(raw)
    assert out is raw and notes == []


def test_rewrite_clamps_budget_to_the_ceiling(clean_state):
    payload = {"max_tokens": 4096, "thinking": {"type": "enabled", "budget_tokens": 9000}}
    out, notes = clean_state.rewrite(body(payload))
    assert json.loads(out)["thinking"]["budget_tokens"] == 3072
    assert notes == ["budget_tokens 9000->3072"]


def test_rewrite_leaves_fitting_budget_alone(clean_state):
    raw = body({"max_tokens": 4096, "thinking": {"type": "enabled", "budget_tokens": 1024}})
    out, notes = clean_state.rewrite(raw)
    assert out is raw and notes == []


# rewrite: passthrough


def test_rewrite_returns_non_dict_bodies_untouched(clean_state):
    for raw in (b"[1, 2]", b"not json at all", b""):
        out, notes = clean_state.rewrite(raw)
        assert out is raw and notes == []


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
    sources.observe({"type": "server_tool_use", "id": "srv_1",
                     "input": {"type": "open_page", "url": "https://a.example/x"}}, 1)
    sources.observe({"type": "server_tool_use", "id": "srv_2",
                     "input": {"type": "search", "query": "y"}}, 2)
    sources.observe({"type": "server_tool_use", "id": "srv_3",
                     "input": {"type": "open_page", "url": "https://a.example/x"}}, 3)
    assert sources.urls == ["https://a.example/x"]
    assert sources.tool_use_id == "srv_1"
    assert sources.max_index == 3


def test_search_sources_block_shape_and_fallback_id(clean_state):
    sources = clean_state.SearchSources()
    assert sources.block() is None
    sources.observe({"type": "server_tool_use",
                     "input": {"type": "open_page", "url": "https://a.example/x"}}, 0)
    block = sources.block()
    assert block["type"] == "web_search_tool_result"
    assert block["tool_use_id"] == "ws_proxy_sources"
    assert block["content"][0]["url"] == "https://a.example/x"


# signature and census: measured shapes, credential-safe by construction


def test_signature_records_shape_bits(clean_state):
    payload = {
        "model": "m", "max_tokens": 4096, "effort": {"level": "max"},
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "tools": [{"type": "web_search_20250305"}, {"name": "bash"}],
    }
    raw = body(payload) + b' {"cache_control": 1} {"type": "image"}'
    sig = clean_state.signature(payload, {"anthropic-beta": "b, a", "x-api-key": "sk-SECRET"}, raw)
    assert "keys=effort,max_tokens,model,thinking,tools" in sig
    assert "tools=custom,web_search_20250305" in sig
    assert "cache_control" in sig and " image" in sig
    assert "max_tokens=4096" in sig
    assert "thinking=enabled+budget" in sig
    assert "effort=" in sig and "anthropic-beta=a,b" in sig
    assert "SECRET" not in sig and "x-api-key" not in sig


def test_census_records_each_shape_once(clean_state, tmp_path):
    payload = {"max_tokens": 4096}
    clean_state.census(payload, {}, body(payload))
    clean_state.census(payload, {}, body(payload))
    assert len(clean_state._shapes) == 1
    assert json.loads((tmp_path / "shapes.json").read_text()) == list(clean_state._shapes)
    assert (tmp_path / "proxy.log").read_text().count("shape: ") == 1


def test_census_never_breaks_a_request(clean_state, tmp_path):
    clean_state.census({"effort": object()}, {}, b"{}")  # effort is not JSON serializable
    assert "census-error" in (tmp_path / "proxy.log").read_text()


def test_census_evicts_oldest_past_the_cap(clean_state, tmp_path, monkeypatch):
    monkeypatch.setattr(clean_state, "SHAPES_MAX", 2)
    for n in range(3):
        payload = {"max_tokens": n}
        clean_state.census(payload, {}, body(payload))
    assert len(clean_state._shapes) == 2
    assert "max_tokens=0" not in " ".join(clean_state._shapes)
    assert json.loads((tmp_path / "shapes.json").read_text()) == sorted(clean_state._shapes)


def test_atomic_write_leaves_valid_json_and_no_tmp(clean_state, tmp_path):
    path = tmp_path / "state.json"
    clean_state._atomic_write_json(str(path), {"b": 1, "a": [1, 2]})
    assert json.loads(path.read_text()) == {"a": [1, 2], "b": 1}
    assert list(tmp_path.glob("*.tmp")) == []


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
    backoff = lambda n: clean_state.transient_backoff(n, None, rand_fn=lambda a, b: 1.0)
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
    assert clean_state._counters["transient_retries_total"] == 1


def test_cooldown_window(clean_state):
    assert clean_state._in_cooldown(now=100.0) is False
    clean_state._enter_cooldown(now=100.0)
    assert clean_state._in_cooldown(now=159.9) is True
    assert clean_state._in_cooldown(now=160.0) is False
    assert clean_state._cooldown_until == 160.0


# remember and load_learned: the persistent half of learn-and-retry


def test_remember_persists_sorted_and_idempotent(clean_state, tmp_path):
    clean_state.remember("zzz_field")
    clean_state.remember("zzz_field")
    stored = json.loads((tmp_path / "learned.json").read_text())
    assert list(stored) == sorted(stored) and "zzz_field" in stored
    assert stored["zzz_field"]["hits"] == 1
    assert stored["zzz_field"]["first_seen"] is not None
    assert clean_state._learned["zzz_field"]["hits"] == 2  # the re-hit counted in memory
    assert (tmp_path / "proxy.log").read_text().count("learned: drop `zzz_field`") == 1


def test_remember_refuses_past_the_cap(clean_state, tmp_path, monkeypatch):
    monkeypatch.setattr(clean_state, "LEARNED_MAX_FIELDS", 2)  # the two seeds fill it
    clean_state.remember("one_field_too_many")
    assert "one_field_too_many" not in clean_state._learned
    assert not (tmp_path / "learned.json").exists()
    assert "learned-full" in (tmp_path / "proxy.log").read_text()


def test_load_learned_merges_seeds_and_survives_garbage(clean_state, tmp_path, monkeypatch):
    monkeypatch.setattr(clean_state, "LEARNED", str(tmp_path / "missing.json"))
    assert set(clean_state.load_learned()) == set(clean_state.SEEDED_DROPS)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setattr(clean_state, "LEARNED", str(bad))
    assert set(clean_state.load_learned()) == set(clean_state.SEEDED_DROPS)


def test_load_learned_migrates_the_old_bare_list(clean_state, tmp_path, monkeypatch):
    old = tmp_path / "old.json"
    old.write_text(json.dumps(["stop_sequences", "safeguards", "taught_field"]))
    monkeypatch.setattr(clean_state, "LEARNED", str(old))
    assert set(clean_state.load_learned()) == {"stop_sequences", "safeguards", "taught_field"}
