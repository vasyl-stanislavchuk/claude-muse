"""Request repair: static rules, the reasoning pipeline, learned drops.

Owns rewrite(), the pure function over request bytes that fixes the shapes
the endpoint rejects and passes everything else through byte for byte.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import re

from . import policy, state
from .policy import Rule
from .reasoning import normalize_reasoning
from .state import log

_warned_ops = set()


def _rule_applies(rule: Rule, model: str | None) -> bool:
    patterns = rule.models
    if not isinstance(patterns, list):
        return False
    return any(isinstance(p, str) and fnmatch.fnmatchcase(model or "", p) for p in patterns)


def _dig(obj, keys):
    for key in keys:
        if not isinstance(obj, dict) or key not in obj:
            return None
        obj = obj[key]
    return obj


def _single_target(payload: dict, path):
    """The (holder, key) a dotted path names, or None when absent.

    Only existing keys: no rule adds a key the client never sent.
    """
    if not isinstance(path, str) or not path or path.startswith("tools"):
        return None
    *parents, key = path.split(".")
    holder = _dig(payload, parents)
    return (holder, key) if isinstance(holder, dict) and key in holder else None


def _where_matches(obj: dict, where) -> bool:
    if not where:
        return True
    if not isinstance(where, dict):
        return False
    for key, cond in where.items():
        value = obj.get(key)
        if isinstance(cond, dict) and "startswith" in cond:
            if not str(value or "").startswith(str(cond["startswith"])):
                return False
        elif value != cond:
            return False
    return True


def _each_tool(payload: dict, where):
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return
    for tool in tools:
        if isinstance(tool, dict) and _where_matches(tool, where):
            yield tool


def _drop_holders(payload: dict, rule: Rule) -> list:
    path = rule.path
    if path == "tools[]":
        return list(_each_tool(payload, rule.where))
    if isinstance(path, str) and path and not path.startswith("tools"):
        target = _dig(payload, path.split("."))
        return [target] if isinstance(target, dict) else []
    return []


def _format_note(template, **values) -> str:
    try:
        return str(template).format(**values)
    except (KeyError, IndexError, ValueError):
        return str(template)


def _op_drop_fields(payload: dict, rule: Rule, notes: list) -> None:
    template = rule.note if rule.note is not None else "-{field}"
    for holder in _drop_holders(payload, rule):
        for field in rule.fields:
            if field in holder:
                del holder[field]
                notes.append(_format_note(template, field=field))


def _op_replace(payload: dict, rule: Rule, notes: list) -> None:
    target = _single_target(payload, rule.path)
    if target is None:
        return
    holder, key = target
    current = holder[key]
    if not isinstance(current, dict):
        return
    when = rule.when
    if any(current.get(k) != v for k, v in when.items()):
        return
    marker = next(iter(when))
    old, new = current.get(marker), rule.value.get(marker)
    holder[key] = copy.deepcopy(rule.value)
    notes.append(
        _format_note(rule.note if rule.note is not None else "{old}->{new}", old=old, new=new)
    )


def _op_drop_if(payload: dict, rule: Rule, notes: list) -> None:
    target = _single_target(payload, rule.path)
    if target is None:
        return
    holder, key = target
    current = holder[key]
    equals = rule.equals
    if isinstance(current, dict) and all(current.get(k) == v for k, v in equals.items()):
        del holder[key]
        notes.append(_format_note(rule.note if rule.note is not None else "dropped"))


def _op_floor(payload: dict, rule: Rule, notes: list) -> None:
    target = _single_target(payload, rule.path)
    if target is None:
        return
    holder, key = target
    current = holder[key]
    if isinstance(current, int) and current < rule.value:
        holder[key] = rule.value
        notes.append(
            _format_note(
                rule.note if rule.note is not None else "{old}->{new}", old=current, new=rule.value
            )
        )


def _op_clamp_below(payload: dict, rule: Rule, notes: list) -> None:
    target = _single_target(payload, rule.path)
    if target is None:
        return
    holder, key = target
    current = holder[key]
    ceiling = payload.get(rule.below)
    floor = rule.floor
    if isinstance(current, int) and isinstance(ceiling, int) and current >= ceiling:
        clamped = max(floor, ceiling - floor)
        holder[key] = clamped
        notes.append(
            _format_note(
                rule.note if rule.note is not None else "{old}->{new}", old=current, new=clamped
            )
        )


def _op_set_value(payload: dict, rule: Rule, notes: list) -> None:
    target = _single_target(payload, rule.path)
    if target is None:
        return
    holder, key = target
    if holder[key] != rule.value:
        old = holder[key]
        holder[key] = copy.deepcopy(rule.value)
        notes.append(
            _format_note(
                rule.note if rule.note is not None else "{old}->{new}", old=old, new=rule.value
            )
        )


_OPS = {
    "drop_fields": _op_drop_fields,
    "replace": _op_replace,
    "drop_if": _op_drop_if,
    "floor": _op_floor,
    "clamp_below": _op_clamp_below,
    "set_value": _op_set_value,
}


def apply_rule(payload: dict, rule: Rule, notes: list, model: str | None) -> None:
    if not _rule_applies(rule, model):
        return
    op = rule.op
    fn = _OPS.get(op)
    if fn is None:
        if op not in _warned_ops:
            _warned_ops.add(op)
            log(f"rules: unknown op `{op}`, ignoring (fix or remove it)")
        return
    fn(payload, rule, notes)


# "`stop_sequences` is not supported", "unknown parameter `safeguards`",
# "web_search field `max_uses` is not supported" all name the field in backticks.
_FIELD = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")


def offending_fields(detail: str) -> list[str]:
    """Every field the error names, not just the first.

    One 400 naming three fields used to cost three round trips; now it costs one.
    tool_choice is excluded because it needs a rewrite rather than a removal, and
    rewrite() owns that.
    """
    seen = []
    for name in _FIELD.findall(detail):
        if name != "tool_choice" and name not in seen:
            seen.append(name)
    return seen


def drop_field(payload: dict, field: str) -> bool:
    dropped = False
    if field in payload:
        del payload[field]
        dropped = True
    for tool in payload.get("tools") or []:
        if isinstance(tool, dict) and field in tool:
            del tool[field]
            dropped = True
    return dropped


def rewrite(body: bytes, drop_tool_choice: bool = False) -> tuple[bytes, list[str]]:
    """Repair the shapes the endpoint rejects, in three layers.

    Static rules first (the YAML file, or the baked-ins when it is missing),
    then the per-model reasoning pipeline, then the fields past 400s taught us,
    then the tool_choice escape hatch the retry loop drives. Anything untouched
    passes through byte for byte.
    """
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body, []
    if not isinstance(payload, dict):
        return body, []

    notes: list[str] = []
    model = payload.get("model")

    for rule in policy._RULES:
        apply_rule(payload, rule, notes, model)
    normalize_reasoning(payload, notes, model)

    for field in sorted(state._learned):
        if drop_field(payload, field):
            notes.append(f"-{field}")

    # offending_fields skips tool_choice, so a 400 naming only it would dead-end
    # the field learner. The retry path degrades it by dropping the key, because
    # absence asserts nothing where auto asserts something.
    if drop_tool_choice and "tool_choice" in payload:
        del payload["tool_choice"]
        notes.append("-tool_choice")

    if not notes:
        return body, []
    return json.dumps(payload).encode(), notes
