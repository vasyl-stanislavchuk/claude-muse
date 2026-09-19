"""Process state, persistence, and observability for the proxy.

Everything the engine remembers between requests lives here: the log writer,
counters, learned fields, calibration, and the JSON files backing them. Other
modules read the live objects through this module (state._learned, never a
copy), because hot reload and the tests rebind them.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

LOG = os.path.expanduser(os.environ.get("CLAUDE_MUSE_PROXY_LOG", "~/.config/claude-muse/proxy.log"))
LEARNED = os.path.expanduser("~/.config/claude-muse/learned.json")

# One generation is enough to answer "what just happened"; the file used to grow
# without bound and was duplicated into proxy.err on top of that.
LOG_MAX_BYTES = 1 << 20

_log_lock = threading.Lock()


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


def _atomic_write_json(path: str, obj: object) -> None:
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


@dataclass
class Counters:
    """Request tallies."""

    requests_total: int = 0
    by_status: dict = field(
        default_factory=lambda: {"2xx": 0, "400": 0, "429": 0, "5xx": 0, "other": 0}
    )
    transient_retries_total: int = 0
    learned_hits_total: int = 0
    # An abandoned request used to be indistinguishable from a served one: both
    # logged 200. Claude Code hanging up mid-classifier is the tell that it gave
    # up waiting and is about to deny the tool call.
    client_hangups_total: int = 0
    # A 2xx carrying no content blocks. This is the failure the max_tokens floor
    # exists to prevent, and nothing proved it stopped happening.
    empty_content_200s: int = 0
    # Requests carrying no reasoning directive. Named from the wire, not from
    # the inference: this shape is the auto-mode classifier, but the proxy can
    # only see the absence.
    no_reasoning_requests_total: int = 0
    stop_reasons: dict = field(default_factory=dict)


_counters = Counters()
_STOP_REASON_MAX = 12
_STARTED = time.monotonic()

# Observed response usage: running totals plus bounded (request_chars,
# input_tokens) samples for the count_tokens calibrator. Replace, never merge:
# a 5xx or a cut stream leaves the last good values untouched.
_usage_totals = {"input_tokens": 0, "output_tokens": 0}
_ratio_samples = deque(maxlen=200)

# Pooled all-time calibration behind the window above, so a restart keeps what
# past traffic taught. Folded forward on every count_tokens answer.
CALIBRATION = os.path.expanduser("~/.config/claude-muse/calibration.json")


@dataclass
class Calibration:
    """Pooled characters-per-token observations behind the live window."""

    chars: int = 0
    tokens: int = 0
    samples: int = 0


def load_calibration() -> Calibration:
    try:
        with open(CALIBRATION) as fh:
            stored = json.load(fh)
    except (OSError, ValueError):
        return Calibration()
    if not isinstance(stored, dict):
        return Calibration()
    clean = {}
    for key in ("chars", "tokens", "samples"):
        value = stored.get(key)
        clean[key] = value if isinstance(value, int) and value >= 0 else 0
    return Calibration(**clean)


_calibration = load_calibration()


def estimate_tokens(chars: int) -> tuple[int, float, int]:
    """(input_tokens, ratio, samples): count_tokens from pooled observations.

    The ratio is characters-per-token over the persisted seed plus this
    process's window, so it improves with use and survives restarts. Answering
    checkpoints the window into the seed; with no observations at all it is
    exactly the chars/4 guess it replaces.
    """
    with _counters_lock:
        window_chars = sum(c for c, _ in _ratio_samples)
        window_tokens = sum(t for _, t in _ratio_samples)
        total_chars = _calibration.chars + window_chars
        total_tokens = _calibration.tokens + window_tokens
        total_samples = _calibration.samples + len(_ratio_samples)
        if total_tokens > 0 and total_chars > 0:
            ratio = total_chars / total_tokens
        else:
            ratio = 4.0
        _calibration.chars = total_chars
        _calibration.tokens = total_tokens
        _calibration.samples = total_samples
        _ratio_samples.clear()
        _atomic_write_json(CALIBRATION, asdict(_calibration))
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
        _counters.requests_total += 1
        _counters.by_status[bucket] += 1


def _request_line(
    rid: str,
    command: str,
    path: str,
    status: int,
    ms: int,
    attempts: int,
    note: str,
    detail: str = "",
) -> str:
    line = f"{rid} {command} {path} {status} {ms}ms attempts={attempts} [{note}]"
    return f"{line} {detail}" if detail else line


# Fields the endpoint has rejected by name at some point. Seeded with the ones
# already measured; the retry loop adds any others it meets, so a new gap costs
# one slow request instead of a debugging session. Stored as field ->
# {first_seen, hits} so a stale entry can be judged before it is removed.
# A dict in memory too: rewrite() only iterates and tests membership, so the
# richer record costs it nothing.
SEEDED_DROPS = ["stop_sequences", "safeguards"]
LEARNED_MAX_FIELDS = 100
_learned_lock = threading.Lock()


@dataclass
class LearnedEntry:
    """One field the endpoint taught us to drop, and how often it fired."""

    first_seen: str | None = None
    hits: int = 0


def load_learned() -> dict[str, LearnedEntry]:
    try:
        with open(LEARNED) as fh:
            stored = json.load(fh)
    except (OSError, ValueError):
        stored = []
    if isinstance(stored, dict):
        learned = {}
        for k, v in stored.items():
            if not isinstance(v, dict):
                continue
            first = v.get("first_seen")
            hits = v.get("hits")
            learned[k] = LearnedEntry(
                first_seen=first if first is None or isinstance(first, str) else None,
                hits=hits if isinstance(hits, int) else 0,
            )
    elif isinstance(stored, list):
        # The pre-P0-2 bare list. Upgrading keeps every field it taught us.
        learned = {k: LearnedEntry() for k in stored if isinstance(k, str)}
    else:
        learned = {}
    for seed in SEEDED_DROPS:
        learned.setdefault(seed, LearnedEntry())
    return learned


_learned = load_learned()

_warned_owned = set()


# Fields the proxy writes itself. The learner must never take one: learned
# drops run after normalize_reasoning in rewrite(), so a single 400 naming one
# of these would delete the key the repair had just set, on every request,
# silently, for as long as learned.json survives.
PROXY_OWNED_FIELDS = {"thinking", "output_config", "effort", "reasoning_effort"}


def remember(field: str) -> None:
    if field in PROXY_OWNED_FIELDS:
        if field not in _warned_owned:
            _warned_owned.add(field)
            log(f"learned: refusing `{field}`, the proxy sets it")
        return
    with _learned_lock:
        record = _learned.get(field)
        if record is not None:
            # Already stripped on the way through, so the endpoint naming it
            # again means the strip did not reach it. Count it, quietly.
            record.hits += 1
            return
        if len(_learned) >= LEARNED_MAX_FIELDS:
            log(f"learned-full: cannot record `{field}`, {LEARNED_MAX_FIELDS} fields kept")
            return
        _learned[field] = LearnedEntry(first_seen=_utcnow(), hits=1)
        _atomic_write_json(LEARNED, {k: asdict(v) for k, v in _learned.items()})
        with _counters_lock:
            _counters.learned_hits_total += 1
    log(f"learned: drop `{field}`")
