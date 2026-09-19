"""Offline vectors for engine.reasoning: profile selection and the pipeline."""


# Profile selection and the reasoning pipeline


def test_profile_for_matches_first_glob(clean_state):
    profiles = {"exact-1": {"a": 1}, "exact-*": {"a": 2}, "*": {"a": 3}}
    assert clean_state._profile_for("exact-1", profiles) == {"a": 1}
    assert clean_state._profile_for("exact-9", profiles) == {"a": 2}
    assert clean_state._profile_for("other", profiles) == {"a": 3}


def test_profile_for_unknown_model_warns_once(clean_state, tmp_path):
    profiles = {"muse-spark-*": dict(clean_state.MODEL_DEFAULTS)}
    assert clean_state._profile_for("stranger-1", profiles) == clean_state.Profile()
    assert clean_state._profile_for("stranger-1", profiles) == clean_state.Profile()
    assert (tmp_path / "proxy.log").read_text().count("unknown model `stranger-1`") == 1
    assert clean_state._profile_for(None, profiles) == clean_state.Profile()
    assert "None" not in (tmp_path / "proxy.log").read_text()  # absent is not unknown


def test_normalize_reasoning_uses_profile_values(clean_state):
    profiles = {
        "*": clean_state.Profile(
            min_max_tokens=100,
            min_thinking_budget=10,
            drop_thinking_disabled=True,
            clamp_budget=True,
        )
    }
    payload = {"max_tokens": 50, "thinking": {"type": "enabled", "budget_tokens": 500}}
    notes = []
    clean_state.normalize_reasoning(payload, notes, "any-model", profiles)
    assert payload["max_tokens"] == 100
    assert payload["thinking"]["budget_tokens"] == 90
    assert notes == ["max_tokens 50->100", "budget_tokens 500->90"]


def test_normalize_reasoning_respects_disabled_pipeline(clean_state):
    profiles = {
        "*": clean_state.Profile(
            min_max_tokens=100,
            min_thinking_budget=10,
            thinking_disabled_as=None,  # no mapping, and dropping is off: the block stays
            drop_thinking_disabled=False,
            clamp_budget=False,
        )
    }
    payload = {"max_tokens": 50, "thinking": {"type": "disabled", "budget_tokens": 500}}
    notes = []
    clean_state.normalize_reasoning(payload, notes, "any-model", profiles)
    assert payload == {"max_tokens": 100, "thinking": {"type": "disabled", "budget_tokens": 500}}
    assert notes == ["max_tokens 50->100"]
