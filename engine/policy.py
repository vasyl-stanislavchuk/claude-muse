"""Repair policy: the YAML file when it parses, baked-ins otherwise.

Owns the static rules, the per-model reasoning profiles, and the hot reload
that picks up hand edits without a restart. A malformed file always degrades
to the baked-in copy, never to a failed request.
"""

from __future__ import annotations

import copy
import os
import threading
from dataclasses import asdict, dataclass, field

from . import state
from .state import load_learned, log


@dataclass
class Rule:
    """One static repair rule. Only path and op are required; every op reads
    its own subset of the rest, and unknown keys never survive from_dict."""

    path: str = ""
    op: str = ""
    models: list = field(default_factory=lambda: ["*"])
    where: dict | None = None
    fields: list | None = None
    when: dict | None = None
    value: int | dict | None = None
    equals: dict | None = None
    below: str | None = None
    floor: int | None = None
    note: str | None = None

    @classmethod
    def from_dict(cls, entry: dict) -> Rule:
        """Build from a validated policy entry, ignoring unknown keys."""
        known = {
            "models",
            "path",
            "where",
            "op",
            "fields",
            "when",
            "value",
            "equals",
            "below",
            "floor",
            "note",
        }
        return cls(**{k: v for k, v in entry.items() if k in known})


# Static repair rules, mirrored from templates/rewrite-rules.yaml. The YAML file
# is the policy when it parses; these are the fallback when it does not, so the
# two must stay identical. A rule names models (fnmatch globs, ["*"] matches
# everything including a missing model), a path, and an op.
BAKED_IN_RULES = [
    Rule(
        models=["*"],
        path="tools[]",
        where={"type": {"startswith": "web_search_"}},
        op="drop_fields",
        fields=["max_uses", "allowed_domains", "blocked_domains"],
        note="-{field}",
    ),
    Rule(
        models=["*"],
        path="tool_choice",
        op="replace",
        when={"type": "tool"},
        value={"type": "auto"},
        note="tool_choice {old}->{new}",
    ),
]

RULES = os.path.expanduser("~/.config/claude-muse/rewrite-rules.yaml")


# Per-model reasoning values, read from the same file under `models:`. Keys are
# fnmatch globs; the first match wins. A model matching nothing gets these
# conservative defaults with a log line.
# thinking_disabled_as: what to send instead of a thinking block the endpoint
# cannot parse. Measured 2026-09-19: `thinking: {"type":"disabled"}` answers
# `400 reasoning_effort 'none' is not supported ... Supported values: [minimal,
# low, medium, high, xhigh, max]`, and output_config.effort takes all of those
# but `minimal`. So the least reasoning this endpoint will do is `low`, and a
# client asking for none gets the nearest thing that exists rather than having
# its instruction deleted. None falls back to drop_thinking_disabled.
@dataclass
class Profile:
    """Per-model reasoning values. The defaults below are the single source;
    MODEL_DEFAULTS is the same values as a dict for the loaders."""

    min_max_tokens: int = 4096
    min_thinking_budget: int = 1024
    thinking_disabled_as: dict | None = field(
        default_factory=lambda: {"output_config": {"effort": "low"}}
    )
    drop_thinking_disabled: bool = True
    clamp_budget: bool = True


MODEL_DEFAULTS = asdict(Profile())
BAKED_IN_PROFILES = {"muse-spark-*": Profile()}
_PROFILE_KEYS = {
    "min_max_tokens": int,
    "min_thinking_budget": int,
    "thinking_disabled_as": dict,
    "drop_thinking_disabled": bool,
    "clamp_budget": bool,
}


def _rule_is_valid(rule: dict) -> bool:
    if "models" in rule and not isinstance(rule.get("models"), list):
        return False
    op = rule.get("op")
    if op == "drop_fields":
        return isinstance(rule.get("fields"), list) and bool(rule.get("fields"))
    if op == "replace":
        return (
            isinstance(rule.get("when"), dict)
            and bool(rule.get("when"))
            and isinstance(rule.get("value"), dict)
        )
    if op == "drop_if":
        return isinstance(rule.get("equals"), dict)
    if op == "floor":
        return isinstance(rule.get("value"), int)
    if op == "clamp_below":
        return isinstance(rule.get("below"), str) and isinstance(rule.get("floor"), int)
    if op == "set_value":
        return "value" in rule
    return True  # unknown ops load; apply warns and skips them


def _read_policy_file() -> tuple[dict | None, str]:
    """The parsed policy doc, or (None, reason) when it is unusable."""
    try:
        import yaml
    except ImportError:
        return None, "pyyaml missing"
    try:
        with open(RULES) as fh:
            doc = yaml.safe_load(fh)
    except OSError:
        return None, f"{RULES} missing"
    except Exception as exc:  # malformed YAML must never fail a request
        return None, f"{RULES} unreadable ({exc})"
    if not isinstance(doc, dict) or doc.get("version") != 1:
        return None, f"{RULES} has no version 1 header"
    return doc, ""


def load_rules() -> list[Rule]:
    """Static repair rules: the YAML file when it parses, baked-ins otherwise."""
    doc, error = _read_policy_file()
    if doc is None:
        log(f"rules: {error}, using baked-in repair rules")
        return list(BAKED_IN_RULES)
    entries = doc.get("rules")
    if not isinstance(entries, list):
        log(f"rules: {RULES} has no rules list, using baked-in repair rules")
        return list(BAKED_IN_RULES)
    rules, bad = [], []
    for index, entry in enumerate(entries):
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("op"), str)
            and isinstance(entry.get("path"), str)
            and _rule_is_valid(entry)
        ):
            rules.append(Rule.from_dict(entry))
        else:
            bad.append(index)
    if bad:
        log(f"rules: skipping invalid entries {bad} in {RULES}")
    return rules


def load_profiles() -> dict[str, Profile]:
    """Per-model reasoning profiles from the same file. Silent fallback: the
    rules loader already logged why the file is unusable."""
    doc, _ = _read_policy_file()
    if doc is None:
        return copy.deepcopy(BAKED_IN_PROFILES)
    profiles = doc.get("models")
    if not isinstance(profiles, dict):
        return copy.deepcopy(BAKED_IN_PROFILES)
    out, dropped = {}, 0
    for name, profile in profiles.items():
        if not (isinstance(name, str) and isinstance(profile, dict)):
            dropped += 1
            continue
        clean = {}
        for key, want in _PROFILE_KEYS.items():
            value = profile.get(key, MODEL_DEFAULTS[key])
            if want is int:
                valid = isinstance(value, int) and not isinstance(value, bool)
            elif want is dict:
                # None is a deliberate setting, not a mistake: it means "fall
                # back to deleting the block", which is what shipped before.
                valid = value is None or isinstance(value, dict)
            else:
                valid = isinstance(value, bool)
            clean[key] = copy.deepcopy(value) if valid else copy.deepcopy(MODEL_DEFAULTS[key])
            if not valid and key in profile:
                dropped += 1
        out[name] = Profile(**clean)
    if dropped:
        log(f"rules: dropped {dropped} invalid model profile entries in {RULES}")
    return out or copy.deepcopy(BAKED_IN_PROFILES)


_RULES = load_rules()
_PROFILES = load_profiles()

_policy_mtimes = {}
_reload_lock = threading.Lock()


def _file_mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _maybe_reload_policy():
    """Pick up hand edits to learned.json and rewrite-rules.yaml without a restart.

    Called at the top of every request: one stat per file, and a reload only when
    the bytes moved. Reloads swap whole objects, so readers never see half a file;
    our own writes compare identical and stay silent.
    """
    global _RULES, _PROFILES
    with _reload_lock:
        for path in (state.LEARNED, RULES):
            mtime = _file_mtime(path)
            if mtime == _policy_mtimes.get(path):
                continue
            _policy_mtimes[path] = mtime
            if path == RULES:
                rules, profiles = load_rules(), load_profiles()
                if rules != _RULES or profiles != _PROFILES:
                    _RULES, _PROFILES = rules, profiles
                    log(f"reloaded {path} ({len(rules)} rules, {len(profiles)} profiles)")
            else:
                learned = load_learned()
                if learned != state._learned:
                    with state._learned_lock:
                        state._learned = learned
                    log(f"reloaded {path} ({len(learned)} fields)")


_policy_mtimes = {state.LEARNED: _file_mtime(state.LEARNED), RULES: _file_mtime(RULES)}
