"""Offline vectors for engine.rewrite. No network, no keychain.

Each test pins behavior the paid probe cannot check cheaply: the exact repair
rules and the learn-from-400 extraction. A new repair lands here first, then
earns a probe row per CLAUDE.md.
"""

import json

from conftest import body

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
        "tools": [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 8,
                "allowed_domains": ["a"],
                "blocked_domains": ["b"],
            }
        ],
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
    clean_state._learned["stop_sequences"] = clean_state.LearnedEntry()
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


def test_rewrite_translates_disabled_thinking(clean_state):
    # The endpoint rejects `thinking: {type: disabled}` outright and names the
    # tiers it does take, of which `low` is the cheapest. Translating beats
    # deleting: deleting turns "do not reason" into "reason however you like".
    payload = {"max_tokens": 4096, "thinking": {"type": "disabled"}}
    out, notes = clean_state.rewrite(body(payload))
    sent = json.loads(out)
    assert "thinking" not in sent
    assert sent["output_config"] == {"effort": "low"}
    assert notes == ['thinking disabled->output_config={"effort": "low"}']


def test_rewrite_merges_into_an_existing_output_config(clean_state):
    payload = {
        "max_tokens": 4096,
        "thinking": {"type": "disabled"},
        "output_config": {"format": "text"},
    }
    out, _ = clean_state.rewrite(body(payload))
    assert json.loads(out)["output_config"] == {"format": "text", "effort": "low"}


def test_rewrite_omits_disabled_thinking_when_mapping_is_null(clean_state):
    # The pre-translation behavior stays reachable for a hand-edited profile.
    profile = dict(clean_state.MODEL_DEFAULTS, thinking_disabled_as=None)
    payload = {"max_tokens": 4096, "thinking": {"type": "disabled"}}
    notes = []
    parsed = json.loads(body(payload))
    clean_state.normalize_reasoning(
        parsed, notes, "muse-spark-1.3", {"muse-spark-*": clean_state.Profile(**profile)}
    )
    assert "thinking" not in parsed
    assert "output_config" not in parsed
    assert notes == ["thinking disabled->omitted"]


def test_rewrite_leaves_other_thinking_types_alone(clean_state):
    # Only the disabled form is translated. `adaptive` is what the main loop
    # sends and the endpoint takes it as it is.
    payload = {"max_tokens": 40000, "thinking": {"type": "adaptive"}}
    out, notes = clean_state.rewrite(body(payload))
    assert json.loads(out)["thinking"] == {"type": "adaptive"}
    assert notes == []


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


def test_apply_rule_model_scoping(clean_state):
    rule = clean_state.Rule(models=["muse-spark-*"], path="max_tokens", op="floor", value=100)
    payload, notes = {"max_tokens": 50}, []
    clean_state.apply_rule(payload, rule, notes, "muse-spark-1.3")
    assert payload["max_tokens"] == 100
    payload, notes = {"max_tokens": 50}, []
    clean_state.apply_rule(payload, rule, notes, "other-model")
    assert payload["max_tokens"] == 50 and notes == []
    clean_state.apply_rule(payload, rule, notes, None)
    assert payload["max_tokens"] == 50  # a missing model matches only "*"


def test_apply_rule_unknown_op_warns_once(clean_state, tmp_path):
    rule = clean_state.Rule(path="x", op="levitate")
    clean_state.apply_rule({}, rule, [], "m")
    clean_state.apply_rule({}, rule, [], "m")
    assert (tmp_path / "proxy.log").read_text().count("unknown op `levitate`") == 1


def test_apply_rule_set_value_never_adds_keys(clean_state):
    rule = clean_state.Rule(path="tier", op="set_value", value="priority", note="tier {old}->{new}")
    payload, notes = {"tier": "standard"}, []
    clean_state.apply_rule(payload, rule, notes, "m")
    assert payload["tier"] == "priority" and notes == ["tier standard->priority"]
    payload, notes = {"other": 1}, []
    clean_state.apply_rule(payload, rule, notes, "m")
    assert payload == {"other": 1} and notes == []


def test_apply_rule_replace_swaps_whole_values(clean_state):
    rule = clean_state.Rule(
        path="tool_choice",
        op="replace",
        when={"type": "tool"},
        value={"type": "auto"},
        note="tool_choice {old}->{new}",
    )
    payload, notes = {"tool_choice": {"type": "tool", "name": "w", "extra": 1}}, []
    clean_state.apply_rule(payload, rule, notes, "m")
    assert payload["tool_choice"] == {"type": "auto"}
    assert notes == ["tool_choice tool->auto"]
    assert payload["tool_choice"] is not rule.value


def test_apply_rule_drop_fields_dotted_holder(clean_state):
    rule = clean_state.Rule(path="thinking", op="drop_fields", fields=["budget_tokens"])
    payload, notes = {"thinking": {"type": "enabled", "budget_tokens": 5}}, []
    clean_state.apply_rule(payload, rule, notes, "m")
    assert payload == {"thinking": {"type": "enabled"}} and notes == ["-budget_tokens"]


def test_rewrite_applies_static_rules_before_learned(clean_state):
    clean_state._learned["stop_sequences"] = clean_state.LearnedEntry()
    out, notes = clean_state.rewrite(body({"max_tokens": 200, "stop_sequences": ["x"]}))
    assert notes == ["max_tokens 200->4096", "-stop_sequences"]


def test_rewrite_with_custom_rules(clean_state, monkeypatch):
    monkeypatch.setattr(
        clean_state, "_RULES", [clean_state.Rule(path="tier", op="set_value", value="priority")]
    )
    out, notes = clean_state.rewrite(body({"tier": "standard"}))
    assert json.loads(out)["tier"] == "priority"
    assert notes == ["standard->priority"]
