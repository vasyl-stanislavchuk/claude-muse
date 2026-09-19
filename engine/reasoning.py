"""The thinking/max_tokens pipeline: values as data, pipeline as code.

Values come from the model's profile in policy.py; the pipeline itself is
fixed, because the three normalizations only make sense together.
"""

from __future__ import annotations

import copy
import fnmatch
import json

from . import policy
from .state import log

# Spark spends its whole budget on thinking before it emits any text, so a request
# sized for a one-word verdict returns empty content with stop_reason max_tokens.
# 4096 clears the 1024 budget floor plus real output; 200 measured empty.
MIN_MAX_TOKENS = 4096

_warned_models = set()


def _profile_for(model: str | None, profiles: dict) -> policy.Profile:
    for pattern, profile in profiles.items():
        if fnmatch.fnmatchcase(model or "", pattern):
            return profile
    if model is None:
        return policy.Profile()
    if model not in _warned_models:
        _warned_models.add(model)
        log(f"rules: unknown model `{model}`, using default reasoning profile")
    return policy.Profile()


def normalize_reasoning(
    payload: dict, notes: list, model: str | None, profiles: dict | None = None
) -> None:
    """Canonical thinking/max_tokens pipeline: one place deciding reasoning shape.

    Values come from the model's profile; the pipeline itself is fixed, because
    these three normalizations only make sense together (a floor without a clamp
    would strand budgets above the ceiling).
    """
    profile = _profile_for(model, profiles if profiles is not None else policy._PROFILES)
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "disabled":
        mapping = profile.thinking_disabled_as
        if isinstance(mapping, dict) and mapping:
            # Translate rather than delete. Deleting is the larger intervention:
            # it turns "do not reason" into "reason however you like", which is
            # how a mechanical side query ends up costing 700 thinking tokens.
            del payload["thinking"]
            for key, value in sorted(mapping.items()):
                if isinstance(value, dict) and isinstance(payload.get(key), dict):
                    payload[key].update(copy.deepcopy(value))
                else:
                    payload[key] = copy.deepcopy(value)
            notes.append(
                "thinking disabled->"
                + ",".join(
                    f"{k}={json.dumps(v, sort_keys=True)}" for k, v in sorted(mapping.items())
                )
            )
            thinking = None
        elif profile.drop_thinking_disabled:
            del payload["thinking"]
            notes.append("thinking disabled->omitted")
            thinking = None
    requested = payload.get("max_tokens")
    if isinstance(requested, int) and requested < profile.min_max_tokens:
        payload["max_tokens"] = profile.min_max_tokens
        notes.append(f"max_tokens {requested}->{profile.min_max_tokens}")
    ceiling = payload.get("max_tokens")
    budget = thinking.get("budget_tokens") if isinstance(thinking, dict) else None
    if (
        profile.clamp_budget
        and isinstance(thinking, dict)
        and isinstance(ceiling, int)
        and isinstance(budget, int)
        and budget >= ceiling
    ):
        clamped = max(profile.min_thinking_budget, ceiling - profile.min_thinking_budget)
        thinking["budget_tokens"] = clamped
        notes.append(f"budget_tokens {budget}->{clamped}")
