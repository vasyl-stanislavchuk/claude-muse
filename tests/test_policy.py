"""Offline vectors for engine.policy: the YAML file and its baked-in mirror."""

import json

# Static repair rules: the YAML policy and its baked-in mirror


def test_template_mirrors_baked_ins(clean_state):
    from pathlib import Path

    import yaml

    template = Path(__file__).resolve().parent.parent / "templates" / "rewrite-rules.yaml"
    doc = yaml.safe_load(template.read_text())
    assert doc["version"] == 1
    assert [clean_state.Rule(**e) for e in doc["rules"]] == clean_state.BAKED_IN_RULES
    profiles = {k: clean_state.Profile(**v) for k, v in doc["models"].items()}
    assert profiles == clean_state.BAKED_IN_PROFILES


def test_load_rules_missing_file_falls_back(clean_state, tmp_path, monkeypatch):
    monkeypatch.setattr(clean_state, "RULES", str(tmp_path / "missing.yaml"))
    assert clean_state.load_rules() == clean_state.BAKED_IN_RULES
    assert "missing" in (tmp_path / "proxy.log").read_text()


def test_load_rules_without_pyyaml_falls_back(clean_state, tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "yaml", None)
    assert clean_state.load_rules() == clean_state.BAKED_IN_RULES
    assert "pyyaml missing" in (tmp_path / "proxy.log").read_text()


def test_load_rules_rejects_garbage_and_wrong_versions(clean_state, tmp_path, monkeypatch):
    monkeypatch.setattr(clean_state, "RULES", str(tmp_path / "rules.yaml"))
    (tmp_path / "rules.yaml").write_text("version: 1\nrules: [unclosed")
    assert clean_state.load_rules() == clean_state.BAKED_IN_RULES
    (tmp_path / "rules.yaml").write_text("version: 2\nrules: []\n")
    assert clean_state.load_rules() == clean_state.BAKED_IN_RULES
    (tmp_path / "rules.yaml").write_text("just a string\n")
    assert clean_state.load_rules() == clean_state.BAKED_IN_RULES
    log = (tmp_path / "proxy.log").read_text()
    assert "unreadable" in log and "no version 1" in log


def test_load_rules_skips_invalid_entries(clean_state, tmp_path, monkeypatch):
    import yaml

    doc = {
        "version": 1,
        "rules": [
            {"models": ["*"], "path": "max_tokens", "op": "floor", "value": 100},
            {"op": "floor"},
            {"path": "x", "op": "floor", "value": "lots"},
            "not a rule",
        ],
    }
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml.safe_dump(doc))
    monkeypatch.setattr(clean_state, "RULES", str(rules_file))
    assert clean_state.load_rules() == [clean_state.Rule(**doc["rules"][0])]
    assert "skipping invalid entries [1, 2, 3]" in (tmp_path / "proxy.log").read_text()


# Reasoning profiles from the same file


def test_load_profiles_missing_file_falls_back_silently(clean_state, tmp_path, monkeypatch):
    monkeypatch.setattr(clean_state, "RULES", str(tmp_path / "missing.yaml"))
    assert clean_state.load_profiles() == clean_state.BAKED_IN_PROFILES
    assert not (tmp_path / "proxy.log").exists()  # the rules loader already said why


def test_load_profiles_parses_and_cleans(clean_state, tmp_path, monkeypatch):
    import yaml

    doc = {
        "version": 1,
        "rules": [],
        "models": {
            "muse-spark-*": {"min_max_tokens": 100},
            "broken": {"min_max_tokens": "lots", "clamp_budget": "yes"},
            "also-broken": [1, 2],
        },
    }
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml.safe_dump(doc))
    monkeypatch.setattr(clean_state, "RULES", str(rules_file))
    profiles = clean_state.load_profiles()
    assert profiles["muse-spark-*"].min_max_tokens == 100
    assert profiles["muse-spark-*"].clamp_budget is True  # missing keys default
    assert profiles["broken"] == clean_state.Profile()  # mistyped keys default
    assert "also-broken" not in profiles
    assert "dropped 3 invalid" in (tmp_path / "proxy.log").read_text()


# Hot reload: hand edits land on the next request, no restart


def test_reload_picks_up_edited_rules(clean_state, tmp_path):
    import yaml

    rules_file = tmp_path / "rewrite-rules.yaml"
    rules_file.write_text(yaml.safe_dump({"version": 1, "rules": [], "models": {}}))
    clean_state._maybe_reload_policy()
    assert clean_state._RULES == []
    assert clean_state._PROFILES == clean_state.BAKED_IN_PROFILES
    assert "reloaded" in (tmp_path / "proxy.log").read_text()
    clean_state._maybe_reload_policy()
    assert (tmp_path / "proxy.log").read_text().count("reloaded") == 1


def test_reload_picks_up_edited_learned(clean_state, tmp_path):
    learned_file = tmp_path / "learned.json"
    learned_file.write_text(json.dumps({"hand_added": {"first_seen": "t", "hits": 1}}))
    clean_state._maybe_reload_policy()
    assert "hand_added" in clean_state._learned
    assert "stop_sequences" in clean_state._learned  # seeds re-merged
    clean_state.remember("fresh_field")  # our own write after that stays silent
    clean_state._maybe_reload_policy()
    assert (tmp_path / "proxy.log").read_text().count("reloaded") == 1


def test_reload_treats_garbage_as_fallback(clean_state, tmp_path, monkeypatch):
    monkeypatch.setattr(clean_state, "_RULES", [{"path": "x", "op": "floor", "value": 1}])
    (tmp_path / "rewrite-rules.yaml").write_text("version: 1\nrules: [oops")
    clean_state._maybe_reload_policy()
    assert clean_state._RULES == clean_state.BAKED_IN_RULES
    log = (tmp_path / "proxy.log").read_text()
    assert "unreadable" in log and "reloaded" in log
