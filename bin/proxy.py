#!/usr/bin/env python3
"""Localhost shim in front of api.meta.ai for claude-muse.

The Meta Model API serves an Anthropic-compatible *subset*. Claude Code sends five
request shapes it rejects, which took out WebSearch and every subagent launch. This
rewrites those five on the way through and passes everything else byte for byte.

It never reads or stores the credential: x-api-key is forwarded as received, so the
key stays in the keychain behind apiKeyHelper.
"""

from __future__ import annotations

import http.client
import copy
import fnmatch
import itertools
import json
import math
import os
import random
import re
import socket
import sys
import hashlib
import threading
import time
from collections import deque
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urlsplit
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("CLAUDE_MUSE_UPSTREAM", "api.meta.ai")
PORT = int(os.environ.get("CLAUDE_MUSE_PROXY_PORT", "8787"))
LOG = os.path.expanduser(os.environ.get("CLAUDE_MUSE_PROXY_LOG", "~/.config/claude-muse/proxy.log"))
LEARNED = os.path.expanduser("~/.config/claude-muse/learned.json")

# Spark spends its whole budget on thinking before it emits any text, so a request
# sized for a one-word verdict returns empty content with stop_reason max_tokens.
# 4096 clears the 1024 budget floor plus real output; 200 measured empty.
MIN_MAX_TOKENS = 4096

# One generation is enough to answer "what just happened"; the file used to grow
# without bound and was duplicated into proxy.err on top of that.
LOG_MAX_BYTES = 1 << 20

# 4096 truncated long errors mid-sentence, so a field named late was unlearnable.
ERROR_BODY_LIMIT = 65536
MAX_REPAIR_ATTEMPTS = 6

# Transient policy: how long one wait may run, how long the proxy stays quiet
# after giving up, and how many waits one request spends before it gives up.
MAX_RETRY_INTERVAL = float(os.environ.get("CLAUDE_MUSE_MAX_RETRY_INTERVAL", "30"))
TRANSIENT_COOLDOWN = float(os.environ.get("CLAUDE_MUSE_TRANSIENT_COOLDOWN", "60"))
MAX_TRANSIENT_WAITS = 3
TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}

# Timeouts split by phase: connecting fails fast, an accepted stream may run
# long, and only a truly runaway one gets cut. Reads re-arm per recv, so the
# read timeout bounds silence between bytes, never the stream itself.
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 600
STREAM_TIMEOUT = float(os.environ.get("CLAUDE_MUSE_STREAM_TIMEOUT", "3600"))

# First event-group budget before stream headers commit: enough for any real
# opening frame, bounded against a pathological one.
BOOTSTRAP_MAX_BYTES = 32768
BOOTSTRAP_TIMEOUT = 5

# An SSE-embedded error retries only on explicit overload language. Anything
# vaguer passes through untouched: a wrong guess here would cool down the proxy
# over an error that was never transient.
_TRANSIENT_HINTS = re.compile(
    r"overloaded|over_?capacity|rate_?limit|rate limit|too many requests|"
    r"temporarily unavailable|server_is_overloaded",
    re.IGNORECASE,
)

SHAPES = os.path.expanduser("~/.config/claude-muse/shapes.json")

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
    "content-encoding", "host",
}

_log_lock = threading.Lock()


def source_hash() -> str:
    """Identity of the code actually running, so preflight can spot a stale proxy."""
    try:
        with open(__file__, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:12]
    except OSError:
        return "unknown"


VERSION = source_hash()


def log(msg: str) -> None:
    """The record goes to proxy.log only.

    It used to also go to stderr, which launchd routes to proxy.err, so the two
    files were near-duplicates. Keeping them separate makes a non-empty proxy.err
    mean "something crashed" instead of "here is a second copy".
    """
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}\n"
    with _log_lock:
        try:
            if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX_BYTES:
                os.replace(LOG, LOG + ".1")
            with open(LOG, "a") as fh:
                fh.write(line)
        except OSError:
            pass


def _atomic_write_json(path: str, obj) -> None:
    """Persist state so a crash mid-write cannot corrupt it.

    Write aside, fsync, rename over: readers see the old file or the new one,
    never half of each. Callers already hold the lock for the state they write.
    """
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Request observability: every request gets an id, a latency line and a tally.
# In memory only, counters never bodies, so the hot path stays credential-safe.
_request_ids = itertools.count(1)
_counters_lock = threading.Lock()
_counters = {
    "requests_total": 0,
    "by_status": {"2xx": 0, "400": 0, "429": 0, "5xx": 0, "other": 0},
    "transient_retries_total": 0,
    "learned_hits_total": 0,
}
_cooldown_until = None  # monotonic timestamp while cooling down, else None
_STARTED = time.monotonic()

# Observed response usage: running totals plus bounded (request_chars,
# input_tokens) samples for the count_tokens calibrator. Replace, never merge:
# a 5xx or a cut stream leaves the last good values untouched.
_usage_totals = {"input_tokens": 0, "output_tokens": 0}
_ratio_samples = deque(maxlen=200)

# Pooled all-time calibration behind the window above, so a restart keeps what
# past traffic taught. Folded forward on every count_tokens answer.
CALIBRATION = os.path.expanduser("~/.config/claude-muse/calibration.json")


def load_calibration() -> dict:
    try:
        with open(CALIBRATION) as fh:
            stored = json.load(fh)
    except (OSError, ValueError):
        return {"chars": 0, "tokens": 0, "samples": 0}
    if not isinstance(stored, dict):
        return {"chars": 0, "tokens": 0, "samples": 0}
    clean = {}
    for key in ("chars", "tokens", "samples"):
        value = stored.get(key)
        clean[key] = value if isinstance(value, int) and value >= 0 else 0
    return clean


_calibration = load_calibration()


def estimate_tokens(chars: int):
    """(input_tokens, ratio, samples): count_tokens from pooled observations.

    The ratio is characters-per-token over the persisted seed plus this
    process's window, so it improves with use and survives restarts. Answering
    checkpoints the window into the seed; with no observations at all it is
    exactly the chars/4 guess it replaces.
    """
    with _counters_lock:
        window_chars = sum(c for c, _ in _ratio_samples)
        window_tokens = sum(t for _, t in _ratio_samples)
        total_chars = _calibration["chars"] + window_chars
        total_tokens = _calibration["tokens"] + window_tokens
        total_samples = _calibration["samples"] + len(_ratio_samples)
        if total_tokens > 0 and total_chars > 0:
            ratio = total_chars / total_tokens
        else:
            ratio = 4.0
        _calibration["chars"] = total_chars
        _calibration["tokens"] = total_tokens
        _calibration["samples"] = total_samples
        _ratio_samples.clear()
        _atomic_write_json(CALIBRATION, _calibration)
        return max(0, math.ceil(chars / ratio)), ratio, total_samples


def _count(status: int) -> None:
    """One request, one tally, by the status the client saw."""
    if 200 <= status <= 299:
        bucket = "2xx"
    elif status == 400:
        bucket = "400"
    elif status == 429:
        bucket = "429"
    elif 500 <= status <= 599:
        bucket = "5xx"
    else:
        bucket = "other"
    with _counters_lock:
        _counters["requests_total"] += 1
        _counters["by_status"][bucket] += 1


def _request_line(rid: str, command: str, path: str, status: int, ms: int,
                  attempts: int, note: str, detail: str = "") -> str:
    line = f"{rid} {command} {path} {status} {ms}ms attempts={attempts} [{note}]"
    return f"{line} {detail}" if detail else line


# Request-scoped failures: the request itself is the problem, so waiting,
# resending or cooling down helps nothing. Matched before any retry decision.
_STOP_STATUSES = {401, 402}
_CONTEXT_STOP = re.compile(
    r"context[^.]{0,40}too[^.]{0,40}long|prompt[^.]{0,40}too[^.]{0,40}long|"
    r"maximum context",
    re.IGNORECASE,
)
_AUTH_STOP = re.compile(
    r"invalid[^.]{0,40}api[^.]{0,40}key|billing_error|unauthorized|authentication",
    re.IGNORECASE,
)


def _is_stop(status: int, snippet: str) -> bool:
    if status in _STOP_STATUSES:
        return True
    text = snippet or ""
    return bool(_CONTEXT_STOP.search(text) or _AUTH_STOP.search(text))


def _stop_hint(status: int, snippet: str) -> str:
    text = snippet or ""
    if _CONTEXT_STOP.search(text):
        return "hint: context exceeds the window; compact or trim and retry"
    if status in _STOP_STATUSES or _AUTH_STOP.search(text):
        return "hint: credential or billing; check the key, not the proxy"
    return ""


def parse_retry_after(value, now=None):
    """Seconds from a Retry-After header, or None when absent or unparseable."""
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d+", value):
        return int(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    base = now or datetime.now(timezone.utc)
    return max(0, int((when - base).total_seconds()))


def transient_backoff(wait_index: int, retry_after=None, rand_fn=random.uniform) -> float:
    """Seconds to wait before resending. A named Retry-After is honored as far
    as the cap allows; otherwise exponential backoff with jitter."""
    if retry_after is not None:
        return min(float(retry_after), MAX_RETRY_INTERVAL)
    return min(2.0 ** wait_index, MAX_RETRY_INTERVAL) * rand_fn(0.5, 1.0)


def _take_transient_wait(waits: list, wait_index: int, label: str,
                       retry_after=None) -> float:
    """Wait out one transient failure, recording it for the log line."""
    wait = transient_backoff(wait_index, retry_after)
    waits.append(f"{label}:{wait:g}s")
    with _counters_lock:
        _counters["transient_retries_total"] += 1
    _sleep(wait)
    return wait


def _is_sse(upstream) -> bool:
    return "text/event-stream" in (upstream.getheader("Content-Type") or "")


def _read_sse_prefix(conn, upstream) -> bytes:
    """The stream's first event-group, read before headers commit.

    Lets a 200-embedded overload error retry like the 429 it behaves as. Bounded
    by group end, byte cap, or a short socket timeout, whichever comes first; a
    slow upstream simply proceeds to stream with whatever arrived.
    """
    prefix = bytearray()
    sock = conn.sock
    if sock is not None:
        sock.settimeout(BOOTSTRAP_TIMEOUT)
    try:
        while b"\n\n" not in prefix and len(prefix) < BOOTSTRAP_MAX_BYTES:
            try:
                chunk = upstream.read(min(8192, BOOTSTRAP_MAX_BYTES - len(prefix)))
            except OSError:
                break
            if not chunk:
                break
            prefix += chunk
    finally:
        if sock is not None:
            sock.settimeout(READ_TIMEOUT)
    return bytes(prefix)


def _sse_prefix_error(prefix: bytes):
    """(is_error, snippet): whether the first event-group reports an error."""
    head, _, _ = prefix.partition(b"\n\n")
    for line in head.split(b"\n"):
        if not line.startswith(b"data: "):
            continue
        text = line[6:].strip()
        if text in (b"[DONE]", b""):
            continue
        try:
            event = json.loads(text)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "error":
            return True, json.dumps(event)[:4096]
    return False, ""


def _deadline_hit(deadline) -> bool:
    """Whether the stream ran past its wall-clock budget, saying so once."""
    if deadline is not None and time.monotonic() > deadline:
        log(f"stream-timeout: closing after {STREAM_TIMEOUT:g}s")
        return True
    return False


def _fold_usage_holder(usage: dict, holder) -> None:
    """Replace usage fields from a response object, never merge."""
    if not isinstance(holder, dict):
        return
    if isinstance(holder.get("model"), str):
        usage["model"] = holder["model"]
    nested = holder.get("usage")
    if not isinstance(nested, dict):
        return
    for key in ("input_tokens", "output_tokens"):
        if isinstance(nested.get(key), int):
            usage[key] = nested[key]


def _note_usage(request_model, usage: dict, request_chars: int, has_tools: bool) -> None:
    """Fold one response's usage into totals and the count_tokens calibrator.

    Only tool-less responses teach the ratio: a tool-using response folds
    whatever the model went and read into its input count, which no estimate
    made beforehand could know. Totals count everything regardless.
    """
    served = usage.get("model")
    if served and request_model and served != request_model:
        log(f"model-substitution requested={request_model} served={served}")
    received = usage.get("input_tokens")
    sent = usage.get("output_tokens")
    if not isinstance(received, int) and not isinstance(sent, int):
        return
    with _counters_lock:
        if isinstance(received, int):
            _usage_totals["input_tokens"] += received
        if isinstance(sent, int):
            _usage_totals["output_tokens"] += sent
        if (request_chars > 0 and isinstance(received, int) and received > 0
                and not has_tools):
            _ratio_samples.append((request_chars, received))


def _in_cooldown(now=None) -> bool:
    if _cooldown_until is None:
        return False
    return (now if now is not None else time.monotonic()) < _cooldown_until


def _enter_cooldown(now=None) -> None:
    global _cooldown_until
    _cooldown_until = (now if now is not None else time.monotonic()) + TRANSIENT_COOLDOWN


_sleep = time.sleep  # indirection so tests observe backoff without waiting


# Static repair rules, mirrored from templates/rewrite-rules.yaml. The YAML file
# is the policy when it parses; these are the fallback when it does not, so the
# two must stay identical. A rule names models (fnmatch globs, ["*"] matches
# everything including a missing model), a path, and an op.
BAKED_IN_RULES = [
    {"models": ["*"], "path": "tools[]",
     "where": {"type": {"startswith": "web_search_"}},
     "op": "drop_fields",
     "fields": ["max_uses", "allowed_domains", "blocked_domains"],
     "note": "-{field}"},
    {"models": ["*"], "path": "tool_choice", "op": "replace",
     "when": {"type": "tool"}, "value": {"type": "auto"},
     "note": "tool_choice {old}->{new}"},
]

RULES = os.path.expanduser("~/.config/claude-muse/rewrite-rules.yaml")
_warned_ops = set()
_warned_models = set()

# Per-model reasoning values, read from the same file under `models:`. Keys are
# fnmatch globs; the first match wins. A model matching nothing gets these
# conservative defaults with a log line.
MODEL_DEFAULTS = {"min_max_tokens": 4096, "min_thinking_budget": 1024,
                  "drop_thinking_disabled": True, "clamp_budget": True}
BAKED_IN_PROFILES = {"muse-spark-*": dict(MODEL_DEFAULTS)}
_PROFILE_KEYS = {"min_max_tokens": int, "min_thinking_budget": int,
                 "drop_thinking_disabled": bool, "clamp_budget": bool}


def _rule_is_valid(rule: dict) -> bool:
    if "models" in rule and not isinstance(rule.get("models"), list):
        return False
    op = rule.get("op")
    if op == "drop_fields":
        return isinstance(rule.get("fields"), list) and bool(rule.get("fields"))
    if op == "replace":
        return (isinstance(rule.get("when"), dict) and bool(rule.get("when"))
                and isinstance(rule.get("value"), dict))
    if op == "drop_if":
        return isinstance(rule.get("equals"), dict)
    if op == "floor":
        return isinstance(rule.get("value"), int)
    if op == "clamp_below":
        return isinstance(rule.get("below"), str) and isinstance(rule.get("floor"), int)
    if op == "set_value":
        return "value" in rule
    return True  # unknown ops load; apply warns and skips them


def _read_policy_file():
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


def load_rules() -> list:
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
        if (isinstance(entry, dict) and isinstance(entry.get("op"), str)
                and isinstance(entry.get("path"), str) and _rule_is_valid(entry)):
            rules.append(entry)
        else:
            bad.append(index)
    if bad:
        log(f"rules: skipping invalid entries {bad} in {RULES}")
    return rules


def load_profiles() -> dict:
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
            else:
                valid = isinstance(value, bool)
            clean[key] = value if valid else MODEL_DEFAULTS[key]
            if not valid and key in profile:
                dropped += 1
        out[name] = clean
    if dropped:
        log(f"rules: dropped {dropped} invalid model profile entries in {RULES}")
    return out or copy.deepcopy(BAKED_IN_PROFILES)


_RULES = load_rules()
_PROFILES = load_profiles()


def _rule_applies(rule: dict, model) -> bool:
    patterns = rule.get("models", ["*"])
    if not isinstance(patterns, list):
        return False
    return any(isinstance(p, str) and fnmatch.fnmatchcase(model or "", p)
               for p in patterns)


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


def _drop_holders(payload: dict, rule: dict) -> list:
    path = rule.get("path", "")
    if path == "tools[]":
        return list(_each_tool(payload, rule.get("where")))
    if isinstance(path, str) and path and not path.startswith("tools"):
        target = _dig(payload, path.split("."))
        return [target] if isinstance(target, dict) else []
    return []


def _format_note(template, **values) -> str:
    try:
        return str(template).format(**values)
    except (KeyError, IndexError, ValueError):
        return str(template)


def _op_drop_fields(payload, rule, notes) -> None:
    template = rule.get("note", "-{field}")
    for holder in _drop_holders(payload, rule):
        for field in rule["fields"]:
            if field in holder:
                del holder[field]
                notes.append(_format_note(template, field=field))


def _op_replace(payload, rule, notes) -> None:
    target = _single_target(payload, rule.get("path"))
    if target is None:
        return
    holder, key = target
    current = holder[key]
    if not isinstance(current, dict):
        return
    when = rule["when"]
    if any(current.get(k) != v for k, v in when.items()):
        return
    marker = next(iter(when))
    old, new = current.get(marker), rule["value"].get(marker)
    holder[key] = copy.deepcopy(rule["value"])
    notes.append(_format_note(rule.get("note", "{old}->{new}"), old=old, new=new))


def _op_drop_if(payload, rule, notes) -> None:
    target = _single_target(payload, rule.get("path"))
    if target is None:
        return
    holder, key = target
    current = holder[key]
    equals = rule["equals"]
    if isinstance(current, dict) and all(current.get(k) == v for k, v in equals.items()):
        del holder[key]
        notes.append(_format_note(rule.get("note", "dropped")))


def _op_floor(payload, rule, notes) -> None:
    target = _single_target(payload, rule.get("path"))
    if target is None:
        return
    holder, key = target
    current = holder[key]
    if isinstance(current, int) and current < rule["value"]:
        holder[key] = rule["value"]
        notes.append(_format_note(rule.get("note", "{old}->{new}"),
                                  old=current, new=rule["value"]))


def _op_clamp_below(payload, rule, notes) -> None:
    target = _single_target(payload, rule.get("path"))
    if target is None:
        return
    holder, key = target
    current = holder[key]
    ceiling = payload.get(rule["below"])
    floor = rule["floor"]
    if isinstance(current, int) and isinstance(ceiling, int) and current >= ceiling:
        clamped = max(floor, ceiling - floor)
        holder[key] = clamped
        notes.append(_format_note(rule.get("note", "{old}->{new}"),
                                  old=current, new=clamped))


def _op_set_value(payload, rule, notes) -> None:
    target = _single_target(payload, rule.get("path"))
    if target is None:
        return
    holder, key = target
    if holder[key] != rule["value"]:
        old = holder[key]
        holder[key] = copy.deepcopy(rule["value"])
        notes.append(_format_note(rule.get("note", "{old}->{new}"),
                                  old=old, new=rule["value"]))


_OPS = {
    "drop_fields": _op_drop_fields,
    "replace": _op_replace,
    "drop_if": _op_drop_if,
    "floor": _op_floor,
    "clamp_below": _op_clamp_below,
    "set_value": _op_set_value,
}


def apply_rule(payload: dict, rule: dict, notes: list, model) -> None:
    if not _rule_applies(rule, model):
        return
    op = rule.get("op")
    fn = _OPS.get(op)
    if fn is None:
        if op not in _warned_ops:
            _warned_ops.add(op)
            log(f"rules: unknown op `{op}`, ignoring (fix or remove it)")
        return
    fn(payload, rule, notes)


def _profile_for(model, profiles):
    for pattern, profile in profiles.items():
        if fnmatch.fnmatchcase(model or "", pattern):
            return profile
    if model is None:
        return MODEL_DEFAULTS
    if model not in _warned_models:
        _warned_models.add(model)
        log(f"rules: unknown model `{model}`, using default reasoning profile")
    return MODEL_DEFAULTS


def normalize_reasoning(payload: dict, notes: list, model, profiles=None) -> None:
    """Canonical thinking/max_tokens pipeline: one place deciding reasoning shape.

    Values come from the model's profile; the pipeline itself is fixed, because
    these three normalizations only make sense together (a floor without a clamp
    would strand budgets above the ceiling).
    """
    profile = _profile_for(model, profiles if profiles is not None else _PROFILES)
    thinking = payload.get("thinking")
    if (profile["drop_thinking_disabled"] and isinstance(thinking, dict)
            and thinking.get("type") == "disabled"):
        del payload["thinking"]
        notes.append("thinking disabled->omitted")
        thinking = None
    requested = payload.get("max_tokens")
    if isinstance(requested, int) and requested < profile["min_max_tokens"]:
        payload["max_tokens"] = profile["min_max_tokens"]
        notes.append(f"max_tokens {requested}->{profile['min_max_tokens']}")
    ceiling = payload.get("max_tokens")
    budget = thinking.get("budget_tokens") if isinstance(thinking, dict) else None
    if (profile["clamp_budget"] and isinstance(thinking, dict)
            and isinstance(ceiling, int) and isinstance(budget, int)
            and budget >= ceiling):
        clamped = max(profile["min_thinking_budget"], ceiling - profile["min_thinking_budget"])
        thinking["budget_tokens"] = clamped
        notes.append(f"budget_tokens {budget}->{clamped}")


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
    global _RULES, _PROFILES, _learned
    with _reload_lock:
        for path in (LEARNED, RULES):
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
                if learned != _learned:
                    with _learned_lock:
                        _learned = learned
                    log(f"reloaded {path} ({len(learned)} fields)")


_policy_mtimes = {LEARNED: _file_mtime(LEARNED), RULES: _file_mtime(RULES)}


# Fields the endpoint has rejected by name at some point. Seeded with the ones
# already measured; the retry loop adds any others it meets, so a new gap costs
# one slow request instead of a debugging session. Stored as field ->
# {first_seen, hits} so a stale entry can be judged before it is removed.
# A dict in memory too: rewrite() only iterates and tests membership, so the
# richer record costs it nothing.
SEEDED_DROPS = ["stop_sequences", "safeguards"]
LEARNED_MAX_FIELDS = 100
_learned_lock = threading.Lock()


def load_learned() -> dict:
    try:
        with open(LEARNED) as fh:
            stored = json.load(fh)
    except (OSError, ValueError):
        stored = []
    if isinstance(stored, dict):
        learned = {k: v for k, v in stored.items() if isinstance(v, dict)}
    elif isinstance(stored, list):
        # The pre-P0-2 bare list. Upgrading keeps every field it taught us.
        learned = {k: {"first_seen": None, "hits": 0} for k in stored if isinstance(k, str)}
    else:
        learned = {}
    for seed in SEEDED_DROPS:
        learned.setdefault(seed, {"first_seen": None, "hits": 0})
    return learned


_learned = load_learned()


def remember(field: str) -> None:
    with _learned_lock:
        record = _learned.get(field)
        if record is not None:
            # Already stripped on the way through, so the endpoint naming it
            # again means the strip did not reach it. Count it, quietly.
            record["hits"] = record.get("hits", 0) + 1
            return
        if len(_learned) >= LEARNED_MAX_FIELDS:
            log(f"learned-full: cannot record `{field}`, {LEARNED_MAX_FIELDS} fields kept")
            return
        _learned[field] = {"first_seen": _utcnow(), "hits": 1}
        _atomic_write_json(LEARNED, _learned)
        with _counters_lock:
            _counters["learned_hits_total"] += 1
    log(f"learned: drop `{field}`")


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

    for rule in _RULES:
        apply_rule(payload, rule, notes, model)
    normalize_reasoning(payload, notes, model)

    for field in sorted(_learned):
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



# Header names the census may record. An allowlist, not a denylist, so the
# credential is excluded by construction rather than by remembering to filter it.
CENSUS_HEADERS = ("anthropic-beta", "anthropic-version", "accept")

_shapes_lock = threading.Lock()

# The census is observations, so eviction is safe: a shape seen again is simply
# recorded again. Insertion-ordered dict as an ordered set; order resets to
# sorted on reload, which is close enough for a cache.
SHAPES_MAX = 500


def load_shapes() -> dict:
    try:
        with open(SHAPES) as fh:
            stored = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(stored, list):
        return {}
    return {s: None for s in stored if isinstance(s, str)}


_shapes = load_shapes()


def signature(payload: dict, headers, raw: bytes = b"") -> str:
    """One line per distinct request shape, so knob decisions are measured.

    Answers, for free, questions that are otherwise guesswork: does
    CLAUDE_CODE_EFFORT_LEVEL actually put an `effort` key on the wire, which beta
    tokens leave the client, is cache_control ever sent, what max_tokens does the
    main loop carry.
    """
    bits = ["keys=" + ",".join(sorted(payload))]

    tools = payload.get("tools") or []
    types = sorted({str(t.get("type", "custom")) for t in tools if isinstance(t, dict)})
    if types:
        bits.append("tools=" + ",".join(types))

    # Substring-search the body as received rather than re-serializing it. These
    # requests carry up to a 1M-token context, so a dumps() per request to find
    # two markers was real latency for nothing.
    if b'"cache_control"' in raw:
        bits.append("cache_control")
    if b'"type": "image"' in raw or b'"type":"image"' in raw:
        bits.append("image")

    bits.append(f"max_tokens={payload.get('max_tokens')}")

    thinking = payload.get("thinking")
    if isinstance(thinking, dict):
        if "budget_tokens" in thinking:
            bits.append(f"thinking={thinking.get('type', 'enabled')}+budget")
        else:
            bits.append("thinking=" + str(thinking.get("type", thinking.get("enabled"))))

    effort = payload.get("effort")
    if effort is not None:
        bits.append("effort=" + json.dumps(effort, sort_keys=True))

    for name in CENSUS_HEADERS:
        value = headers.get(name)
        if value:
            bits.append(f"{name}=" + ",".join(sorted(v.strip() for v in value.split(","))))

    return " ".join(bits)


def census(payload: dict, headers, raw: bytes = b"") -> None:
    try:
        sig = signature(payload, headers, raw)
    except Exception as exc:  # a census must never break a request
        log(f"census-error {type(exc).__name__}: {exc}")
        return
    with _shapes_lock:
        if sig in _shapes:
            return
        _shapes[sig] = None
        while len(_shapes) > SHAPES_MAX:
            _shapes.pop(next(iter(_shapes)))
        _atomic_write_json(SHAPES, sorted(_shapes))
    log(f"shape: {sig}")


def wants_web_search(body: bytes) -> bool:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return any(
        isinstance(t, dict) and str(t.get("type", "")).startswith("web_search_")
        for t in payload.get("tools") or []
    )


def title_from_url(url: str) -> str:
    """A readable label from the URL itself.

    The endpoint never sends titles, and inventing one would be a fabricated
    citation. The host plus the last path segment is honest and useful.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = parts.netloc.removeprefix("www.")
    segment = [s for s in parts.path.split("/") if s]
    if not segment:
        return host
    tail = unquote(segment[-1])
    for suffix in (".html", ".htm", ".md", ".php"):
        tail = tail.removesuffix(suffix)
    tail = tail.replace("-", " ").replace("_", " ").strip()
    return f"{host} — {tail}" if tail else host


class SearchSources:
    """Harvests the URLs the model opened, and builds the block Claude Code wants.

    api.meta.ai runs the search but never returns `web_search_tool_result` blocks,
    so Claude Code has nothing to render as sources and no URL to follow up with.
    The URLs are in the response all the same: every page the model read arrives as
    a server_tool_use whose input is {"type": "open_page", "url": ...}. Collecting
    those and handing them back in the block Claude Code already knows how to parse
    restores both the source list and the search-then-fetch loop.
    """

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.tool_use_id: str | None = None
        self.max_index = -1

    def observe(self, block: dict, index: int | None) -> None:
        if isinstance(index, int):
            self.max_index = max(self.max_index, index)
        if block.get("type") != "server_tool_use":
            return
        if self.tool_use_id is None and isinstance(block.get("id"), str):
            self.tool_use_id = block["id"]
        payload = block.get("input")
        if not isinstance(payload, dict):
            return
        url = payload.get("url")
        if payload.get("type") == "open_page" and isinstance(url, str) and url not in self.urls:
            self.urls.append(url)

    def block(self) -> dict | None:
        if not self.urls:
            return None
        return {
            "type": "web_search_tool_result",
            "tool_use_id": self.tool_use_id or "ws_proxy_sources",
            "content": [
                {"type": "web_search_result", "title": title_from_url(u), "url": u}
                for u in self.urls
            ],
        }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "claude-muse-proxy"

    def log_message(self, fmt, *args):  # quieter than the default stderr spray
        pass

    def handle_one_request(self):
        """Claude Code closing a keep-alive socket is normal, not an incident.

        The idle read happens before _relay's try block, so these used to surface
        as full tracebacks in proxy.err — 19 of the 21 there.
        """
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, BrokenPipeError) as exc:
            self.close_connection = True
            log(f"client-hung-up {type(exc).__name__}")

    def _send_error(self, status: int, message: str) -> None:
        payload = json.dumps(
            {"type": "error", "error": {"type": "api_error", "message": message}}
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_buffered(self, status: int, body: bytes, content_type: str) -> None:
        """Answer from bytes already read, when the retry loop consumed the stream
        deciding what to do. The client sees the upstream's own error shape."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _serve_count_tokens(self, rid: str, start: float, original: bytes) -> None:
        """Answer count_tokens from the calibrated ratio. Zero upstream attempts;
        the log line carries the ratio and sample count behind the number."""
        tokens, ratio, samples = estimate_tokens(len(original))
        ms = int((time.monotonic() - start) * 1000)
        log(_request_line(rid, self.command, self.path, 200, ms, 0, "estimated",
                          f"input_tokens={tokens} ratio={ratio:.2f} samples={samples}"))
        _count(200)
        payload = json.dumps({"input_tokens": tokens}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def _upstream(self, body: bytes):
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        # Compression would have to be undone before it could be re-chunked, and the
        # stream matters more than the bytes saved.
        headers["Accept-Encoding"] = "identity"
        if body:
            headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPSConnection(UPSTREAM, timeout=CONNECT_TIMEOUT)
        conn.connect()  # fail fast on network/DNS/TLS, before any patience applies
        if conn.sock is not None:
            conn.sock.settimeout(READ_TIMEOUT)
        conn.request(self.command, self.path, body=body or None, headers=headers)
        return conn, conn.getresponse()

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _pump_sse(self, upstream, sources: "SearchSources", initial: bytes = b"",
                deadline=None) -> None:
        """Pass the stream through event by event, then append the sources block.

        Events are emitted whole rather than byte by byte, so the injected block can
        go in ahead of the terminating message_delta without splitting an event. The
        blank line that ends each event arrives with it, so nothing is held back.
        `initial` replays bytes the bootstrap read before headers committed.
        Returns the last usage block seen. `sources` may be None, which skips
        injection and only observes.
        """
        buf = initial
        group: list[bytes] = []
        injected = False
        usage: dict = {}

        def flush_group() -> None:
            nonlocal group
            for raw in group:
                self._chunk(raw)
            group = []

        def inject() -> None:
            nonlocal injected
            injected = True
            block = sources.block()
            if block is None:
                return
            index = sources.max_index + 1
            for event, payload in (
                ("content_block_start",
                 {"type": "content_block_start", "index": index, "content_block": block}),
                ("content_block_stop", {"type": "content_block_stop", "index": index}),
            ):
                self._chunk(
                    f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()
                )
            log(f"injected {len(block['content'])} web search sources")

        while True:
            if _deadline_hit(deadline):
                break
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                group.append(line + b"\n")
                if line.strip():
                    continue
                # Blank line: the event is complete.
                kind = None
                for raw in group:
                    if raw.startswith(b":"):
                        continue  # comment/ping frame, passed through below
                    if not raw.startswith(b"data: "):
                        continue
                    text = raw[6:].strip()
                    if text == b"[DONE]":
                        continue  # end marker, passed through below
                    try:
                        event = json.loads(text)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    kind = event.get("type")
                    if kind == "message_start":
                        _fold_usage_holder(usage, event.get("message"))
                        _fold_usage_holder(usage, event)
                    elif kind == "message_delta":
                        _fold_usage_holder(usage, event)
                    elif kind == "content_block_start" and sources is not None:
                        sources.observe(event.get("content_block") or {}, event.get("index"))
                if (sources is not None and kind in ("message_delta", "message_stop")
                        and not injected):
                    inject()
                flush_group()
            chunk = upstream.read(8192)
            if not chunk:
                break
            buf += chunk
        if buf:
            group.append(buf)
        if sources is not None and not injected:
            inject()
        flush_group()
        return usage

    def _pump_json(self, upstream, sources: "SearchSources", deadline=None) -> dict:
        """Buffer one JSON body, observe and inject, serve it whole.

        Responses are output-bounded, so holding one is cheap; requests are
        context-bounded, which is why the census never re-serializes them.
        `sources` may be None, which skips injection and only observes.
        """
        raw = bytearray()
        while True:
            if _deadline_hit(deadline):
                break
            chunk = upstream.read(65536)
            if not chunk:
                break
            raw += chunk
        raw = bytes(raw)
        usage: dict = {}
        try:
            payload = json.loads(raw)
        except ValueError:
            if raw:
                self._chunk(raw)
            return usage
        _fold_usage_holder(usage, payload)
        if sources is not None:
            for index, block in enumerate(payload.get("content") or []):
                if isinstance(block, dict):
                    sources.observe(block, index)
            block = sources.block()
            if block is not None:
                payload.setdefault("content", []).append(block)
                log(f"injected {len(block['content'])} web search sources")
                raw = json.dumps(payload).encode()
        self._chunk(raw)
        return usage

    def _health(self) -> None:
        with _counters_lock:
            by_status = dict(_counters["by_status"])
            requests_total = _counters["requests_total"]
            transient_retries = _counters["transient_retries_total"]
            learned_hits = _counters["learned_hits_total"]
            usage_totals = dict(_usage_totals)
        payload = json.dumps({
            "ok": True,
            "version": VERSION,
            "pid": os.getpid(),
            "upstream": UPSTREAM,
            "port": PORT,
            "uptime_s": int(time.monotonic() - _STARTED),
            "requests": requests_total,
            "by_status": by_status,
            "transient_retries": transient_retries,
            "learned_hits": learned_hits,
            "usage": usage_totals,
            "cooldown_until": _cooldown_until,
            "min_max_tokens": MIN_MAX_TOKENS,
            "learned": sorted(_learned),
            "shapes": len(_shapes),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _relay(self) -> None:
        _maybe_reload_policy()
        if self.path.split("?")[0] == "/__health":
            self._health()
            return
        rid = f"r{next(_request_ids)}"
        start = time.monotonic()
        deadline = start + STREAM_TIMEOUT
        length = int(self.headers.get("Content-Length") or 0)
        original = self.rfile.read(length) if length else b""
        rewritable = self.command == "POST" and self.path.startswith("/v1/messages")

        if rewritable and self.path.split("?")[0] == "/v1/messages/count_tokens":
            # Served locally: the endpoint bills this one 402, and its body is the
            # whole prompt, so neither rewriting nor census has anything to add.
            self._serve_count_tokens(rid, start, original)
            return

        body, notes = rewrite(original) if rewritable else (original, [])

        try:
            original_payload = json.loads(original) if rewritable else None
        except (ValueError, UnicodeDecodeError):
            original_payload = None
        if rewritable and original_payload is not None:
            census(original_payload, self.headers, original)
        request_model = (original_payload.get("model")
                         if isinstance(original_payload, dict) else None)
        request_tools = (bool(original_payload.get("tools"))
                         if isinstance(original_payload, dict) else False)

        # Two retries share this budget of upstream sends. A 400 that names a field
        # is the endpoint teaching us its subset: learn it, strip it, resend. A
        # transient failure (429, 503, a dropped connection) is the endpoint asking
        # for patience: wait and resend unchanged. Nothing has been written to the
        # client yet in either case, so both retries are invisible.
        drop_choice = False
        detail = ""
        attempts = 0
        waits = []
        transient_waits = 0
        sse_pending = None

        def full_note():
            note = " ".join(notes) if notes else "-"
            if waits:
                note += " transient " + "+".join(waits)
            return note

        def stop_and_serve(status, raw_detail, ctype):
            """A request-scoped failure: no retry, no cooldown, upstream's own bytes."""
            text = raw_detail.decode("utf8", "replace")
            hint = _stop_hint(status, text)
            stopped = text.strip() + (f" [{hint}]" if hint else "")
            ms = int((time.monotonic() - start) * 1000)
            log(_request_line(rid, self.command, self.path, status, ms, attempts,
                              full_note(), stopped))
            _count(status)
            self._serve_buffered(status, raw_detail, ctype)

        for _ in range(MAX_REPAIR_ATTEMPTS):
            attempts += 1
            if _in_cooldown():
                ms = int((time.monotonic() - start) * 1000)
                log(_request_line(rid, self.command, self.path, 503, ms, attempts,
                                  "cooldown"))
                _count(503)
                self._send_error(503, "claude-muse proxy: upstream cooling down "
                                      "after repeated failures; retry shortly")
                return
            try:
                conn, upstream = self._upstream(body)
            except OSError as exc:
                if transient_waits < MAX_TRANSIENT_WAITS:
                    _take_transient_wait(waits, transient_waits, "conn")
                    transient_waits += 1
                    continue
                _enter_cooldown()
                ms = int((time.monotonic() - start) * 1000)
                log(f"{rid} {self.command} {self.path} upstream-error "
                    f"{ms}ms attempts={attempts} [{full_note()}] {exc}")
                _count(502)
                self._send_error(502, f"claude-muse proxy: {exc}")
                return
            if upstream.status in TRANSIENT_STATUSES:
                retry_after = upstream.getheader("Retry-After")
                ctype = upstream.getheader("Content-Type") or "application/json"
                raw_detail = upstream.read(ERROR_BODY_LIMIT)
                detail = raw_detail.decode("utf8", "replace")
                conn.close()
                if _is_stop(upstream.status, detail):
                    stop_and_serve(upstream.status, raw_detail, ctype)
                    return
                if transient_waits < MAX_TRANSIENT_WAITS:
                    _take_transient_wait(waits, transient_waits, str(upstream.status),
                                         parse_retry_after(retry_after))
                    transient_waits += 1
                    continue
                _enter_cooldown()
                ms = int((time.monotonic() - start) * 1000)
                log(_request_line(rid, self.command, self.path, upstream.status,
                                  ms, attempts, full_note(), detail.strip()))
                _count(upstream.status)
                self._serve_buffered(upstream.status, raw_detail, ctype)
                return
            if upstream.status != 400 or not rewritable:
                if _is_sse(upstream):
                    prefix = _read_sse_prefix(conn, upstream)
                    sse_pending = prefix
                    is_error, snippet = _sse_prefix_error(prefix)
                    retryable = (is_error and not _is_stop(upstream.status, snippet)
                                 and _TRANSIENT_HINTS.search(snippet))
                    if retryable and transient_waits < MAX_TRANSIENT_WAITS:
                        conn.close()
                        sse_pending = None
                        _take_transient_wait(waits, transient_waits, "sse")
                        transient_waits += 1
                        continue
                    if retryable:
                        _enter_cooldown()
                    if is_error:
                        # Passed through, not terminated on: ending the stream here
                        # would truncate one the client may still be reading.
                        log(f"sse-error {snippet[:500]}")
                break
            ctype = upstream.getheader("Content-Type") or "application/json"
            raw_detail = upstream.read(ERROR_BODY_LIMIT)
            detail = raw_detail.decode("utf8", "replace")
            conn.close()
            if _is_stop(upstream.status, detail):
                # A 400 the request itself caused, most often context over the
                # window. Learning has nothing to teach here; hand back the
                # endpoint's own words plus where to look.
                stop_and_serve(upstream.status, raw_detail, ctype)
                return
            fields = offending_fields(detail)
            if fields:
                for field in fields:
                    remember(field)
            elif "tool_choice" in detail and not drop_choice:
                # offending_fields skips tool_choice, so a 400 naming only it would
                # dead-end as "unnamed". Drop the key rather than forcing auto.
                drop_choice = True
            else:
                ms = int((time.monotonic() - start) * 1000)
                log(_request_line(rid, self.command, self.path, 400, ms, attempts,
                                  f"unnamed {full_note()}", detail.strip()))
                _count(400)
                self._send_error(400, detail.strip() or "claude-muse proxy: upstream rejected the request")
                return
            retried, extra = rewrite(original, drop_tool_choice=drop_choice)
            if retried == body:
                ms = int((time.monotonic() - start) * 1000)
                log(_request_line(rid, self.command, self.path, 400, ms, attempts,
                                  f"unrecoverable {full_note()}", detail.strip()))
                _count(400)
                self._send_error(400, detail.strip() or "claude-muse proxy: upstream rejected the request")
                return
            body, notes = retried, extra
        else:
            # The budget ran out with the last rewrite never sent. Say so, rather
            # than falling through to report a 400 whose body was already consumed
            # off a connection that is now closed.
            ms = int((time.monotonic() - start) * 1000)
            log(_request_line(rid, self.command, self.path, 400, ms, attempts,
                              f"attempts-exhausted {full_note()}", detail.strip()))
            _count(400)
            self._send_error(400, detail.strip() or "claude-muse proxy: repair budget exhausted")
            return

        failed = upstream.status >= 300

        sources = SearchSources() if (rewritable and not failed and wants_web_search(body)) else None

        captured = bytearray()
        usage: dict = {}
        try:
            # Inside the try: the client can vanish between the upstream response
            # and these headers, which is the other two tracebacks in proxy.err.
            self.send_response(upstream.status)
            for key, value in upstream.getheaders():
                if key.lower() not in HOP_BY_HOP:
                    self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if not failed and _is_sse(upstream):
                usage = self._pump_sse(upstream, sources, sse_pending or b"", deadline)
            elif not failed:
                usage = self._pump_json(upstream, sources, deadline)
            else:
                if sse_pending:
                    self._chunk(sse_pending)
                while True:
                    if _deadline_hit(deadline):
                        break
                    chunk = upstream.read(8192)
                    if not chunk:
                        break
                    if failed and len(captured) < 2048:
                        captured.extend(chunk)
                    self._chunk(chunk)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            ms = int((time.monotonic() - start) * 1000)
            log(f"{rid} {self.command} {self.path} {upstream.status} "
                f"{ms}ms attempts={attempts} client-hung-up [{full_note()}]")
            _count(upstream.status)
            conn.close()
            return
        finally:
            conn.close()

        ms = int((time.monotonic() - start) * 1000)
        _note_usage(request_model, usage, len(original), request_tools)
        if failed and self.path == "/api/hello":
            # /api/hello is Claude Code's reachability ping; api.meta.ai has never
            # served it, so its 404 is background noise rather than a finding.
            # Uncounted as well as unlogged, so the tallies stay about real traffic.
            return
        _count(upstream.status)
        usage_bits = " ".join(
            f"{short}={usage[key]}" for key, short in
            (("input_tokens", "in"), ("output_tokens", "out"))
            if isinstance(usage.get(key), int)
        )
        if failed:
            detail = bytes(captured).decode("utf8", "replace").strip()
            log(_request_line(rid, self.command, self.path, upstream.status, ms,
                              attempts, full_note(), detail))
        else:
            log(_request_line(rid, self.command, self.path, upstream.status, ms,
                              attempts, full_note(), usage_bits))

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _relay


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        log(f"handler-error {type(exc).__name__}: {exc}")


def main() -> None:
    server = Server(("127.0.0.1", PORT), Handler)
    log(f"listening on 127.0.0.1:{PORT} -> {UPSTREAM} (version {VERSION})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
