"""Offline vectors for engine.state: counters, learned fields, census, calibration."""

import json

from conftest import body

# Counters and the log line


def test_count_buckets_statuses(clean_state):
    for status, _bucket in [
        (200, "2xx"),
        (400, "400"),
        (429, "429"),
        (503, "5xx"),
        (401, "other"),
        (404, "other"),
    ]:
        clean_state._count(status)
    assert clean_state._counters.requests_total == 6
    assert clean_state._counters.by_status == {
        "2xx": 1,
        "400": 1,
        "429": 1,
        "5xx": 1,
        "other": 2,
    }


def test_request_line_format(clean_state):
    line = clean_state._request_line
    assert (
        line("r7", "POST", "/v1/messages", 200, 812, 2, "-max_uses")
        == "r7 POST /v1/messages 200 812ms attempts=2 [-max_uses]"
    )
    assert line("r7", "POST", "/v1/x", 502, 3, 1, "-", "boom").endswith("[-] boom")


# remember and load_learned: the persistent half of learn-and-retry


def test_remember_refuses_proxy_owned_fields(clean_state):
    # Learned drops run after normalize_reasoning, so learning one of these
    # would delete the key the repair had just set, on every later request.
    for field in ("output_config", "thinking", "effort", "reasoning_effort"):
        clean_state.remember(field)
        assert field not in clean_state._learned
    clean_state.remember("top_k")
    assert "top_k" in clean_state._learned


def test_remember_counts_new_fields_only(clean_state):
    clean_state.remember("fresh_field")
    clean_state.remember("fresh_field")
    assert clean_state._counters.learned_hits_total == 1


def test_remember_persists_sorted_and_idempotent(clean_state, tmp_path):
    clean_state.remember("zzz_field")
    clean_state.remember("zzz_field")
    stored = json.loads((tmp_path / "learned.json").read_text())
    assert list(stored) == sorted(stored) and "zzz_field" in stored
    assert stored["zzz_field"]["hits"] == 1
    assert stored["zzz_field"]["first_seen"] is not None
    assert clean_state._learned["zzz_field"].hits == 2  # the re-hit counted in memory
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


# signature and census: measured shapes, credential-safe by construction


def test_signature_records_shape_bits(clean_state):
    payload = {
        "model": "m",
        "max_tokens": 4096,
        "effort": {"level": "max"},
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


# Calibration: the count_tokens ratio pool


def test_load_calibration_survives_anything(clean_state, tmp_path, monkeypatch):
    assert clean_state.load_calibration() == clean_state.Calibration()
    cal = tmp_path / "calibration.json"
    cal.write_text("{nope")
    assert clean_state.load_calibration() == clean_state.Calibration()
    cal.write_text(json.dumps({"chars": 100, "tokens": -5, "samples": "many"}))
    assert clean_state.load_calibration() == clean_state.Calibration(chars=100, tokens=0, samples=0)
    cal.write_text(json.dumps({"chars": 100, "tokens": 25, "samples": 3}))
    assert clean_state.load_calibration() == clean_state.Calibration(
        chars=100, tokens=25, samples=3
    )


def test_estimate_tokens_pools_and_checkpoints(clean_state, tmp_path):
    assert clean_state.estimate_tokens(100) == (25, 4.0, 0)  # the guess it replaces
    clean_state._ratio_samples.append((300, 100))
    tokens, ratio, samples = clean_state.estimate_tokens(150)
    assert (tokens, ratio, samples) == (50, 3.0, 1)
    assert list(clean_state._ratio_samples) == []  # folded into the seed
    assert json.loads((tmp_path / "calibration.json").read_text()) == {
        "chars": 300,
        "tokens": 100,
        "samples": 1,
    }
    assert clean_state.estimate_tokens(150) == (50, 3.0, 1)  # seed survives alone


# _atomic_write_json: crash-safe writes, no tmp leftovers


def test_atomic_write_leaves_valid_json_and_no_tmp(clean_state, tmp_path):
    path = tmp_path / "state.json"
    clean_state._atomic_write_json(str(path), {"b": 1, "a": [1, 2]})
    assert json.loads(path.read_text()) == {"a": [1, 2], "b": 1}
    assert list(tmp_path.glob("*.tmp")) == []
