"""Upstream traffic: transient policy, stream helpers, usage, sources.

Owns everything between the proxy and api.meta.ai that is not the Handler
itself: which failures wait and resend, the SSE bootstrap peek, usage folding
for the log line, and the web-search sources harvest.
"""

from __future__ import annotations

import json
import os
import random
import re
import select
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urlsplit

from . import state
from .state import log

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


def parse_retry_after(value: str | None, now: datetime | None = None) -> int | None:
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
    return min(2.0**wait_index, MAX_RETRY_INTERVAL) * rand_fn(0.5, 1.0)


def _take_transient_wait(waits: list, wait_index: int, label: str, retry_after=None) -> float:
    """Wait out one transient failure, recording it for the log line."""
    wait = transient_backoff(wait_index, retry_after)
    waits.append(f"{label}:{wait:g}s")
    with state._counters_lock:
        state._counters.transient_retries_total += 1
    _sleep(wait)
    return wait


def _is_sse(upstream) -> bool:
    return "text/event-stream" in (upstream.getheader("Content-Type") or "")


def _read_sse_prefix(conn, upstream) -> bytes:
    """The stream's first event-group, read before headers commit.

    Lets a 200-embedded overload error retry like the 429 it behaves as. Bounded
    by group end, byte cap, or a short wait, whichever comes first; a slow
    upstream simply proceeds to stream with whatever arrived.
    """
    prefix = bytearray()
    sock = conn.sock
    deadline = time.monotonic() + BOOTSTRAP_TIMEOUT
    # Wait with select rather than a socket timeout. A read that times out
    # poisons CPython's buffered reader for good - every later read raises
    # `cannot read from timed out object` - so arming one here used to kill the
    # stream it was supposed to protect. Spark reasons for seconds before the
    # first token, which made that the common case rather than the rare one.
    while b"\n\n" not in prefix and len(prefix) < BOOTSTRAP_MAX_BYTES:
        if sock is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select([sock], [], [], remaining)
            except (OSError, ValueError, TypeError):
                # Closed, or not something select can wait on. Either way the
                # peek is optional: fall through and let the pump stream it.
                break
            if not ready:
                break  # slow first token: stream it normally, socket untouched
        want = min(8192, BOOTSTRAP_MAX_BYTES - len(prefix))
        try:
            # read1 returns what has arrived; read would block for the full
            # count and undo the point of selecting first.
            chunk = upstream.read1(want) if hasattr(upstream, "read1") else upstream.read(want)
        except OSError:
            break
        if not chunk:
            break
        prefix += chunk
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
    details = nested.get("output_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("thinking_tokens"), int):
        usage["thinking_tokens"] = details["thinking_tokens"]


def _fold_stop_reason(usage: dict, holder) -> None:
    """Take stop_reason from a JSON body or an SSE message_delta.

    The str guard is load-bearing: message_start carries stop_reason null, and
    folding that would wipe the real value the delta brings later.
    """
    if not isinstance(holder, dict):
        return
    for candidate in (holder, holder.get("delta")):
        if isinstance(candidate, dict) and isinstance(candidate.get("stop_reason"), str):
            usage["stop_reason"] = candidate["stop_reason"]


def _tally_block(usage: dict, block) -> None:
    """Count one content block by type. Types only, never text.

    Kept out of _fold_usage_holder because that one replaces and this one
    accumulates; mixing the two contracts is how a retry double-counts.
    """
    if not isinstance(block, dict) or not isinstance(block.get("type"), str):
        return
    blocks = usage.setdefault("blocks", {})
    blocks[block["type"]] = blocks.get(block["type"], 0) + 1


def _note_usage(
    request_model: str | None, usage: dict, request_chars: int, has_tools: bool
) -> None:
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
    with state._counters_lock:
        if isinstance(received, int):
            state._usage_totals["input_tokens"] += received
        if isinstance(sent, int):
            state._usage_totals["output_tokens"] += sent
        if request_chars > 0 and isinstance(received, int) and received > 0 and not has_tools:
            state._ratio_samples.append((request_chars, received))


def _outcome_bits(usage: dict, no_reasoning: bool) -> str:
    """The end of the log line: why it stopped, what it contained.

    blocks= is emitted even when empty, as `blocks=none`, because an absent
    field cannot be grepped for and an empty response is the thing worth
    finding. `no-reasoning` is a bare token for the same reason.
    """
    bits = []
    stop = usage.get("stop_reason")
    if isinstance(stop, str):
        bits.append(f"stop={stop}")
    blocks = usage.get("blocks") or {}
    bits.append(
        "blocks=" + (",".join(f"{k}:{v}" for k, v in sorted(blocks.items())) if blocks else "none")
    )
    if no_reasoning:
        bits.append("no-reasoning")
    return " ".join(bits)


def _note_outcome(usage: dict, status: int, failed: bool) -> None:
    """Tally stop reasons and empty 2xx bodies."""
    with state._counters_lock:
        stop = usage.get("stop_reason")
        if isinstance(stop, str):
            reasons = state._counters.stop_reasons
            if stop in reasons or len(reasons) < state._STOP_REASON_MAX:
                reasons[stop] = reasons.get(stop, 0) + 1
        if not failed and 200 <= status < 300 and not (usage.get("blocks") or {}):
            state._counters.empty_content_200s += 1


def _in_cooldown(now=None) -> bool:
    if _cooldown_until is None:
        return False
    return (now if now is not None else time.monotonic()) < _cooldown_until


def _enter_cooldown(now=None) -> None:
    global _cooldown_until
    _cooldown_until = (now if now is not None else time.monotonic()) + TRANSIENT_COOLDOWN


_cooldown_until = None  # monotonic timestamp while cooling down, else None

_sleep = time.sleep  # indirection so tests observe backoff without waiting


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
