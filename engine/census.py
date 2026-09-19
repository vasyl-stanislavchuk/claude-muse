"""Request shape census: measured answers to knob questions, for free.

Records one line per distinct request shape so decisions stay measured: which
beta tokens leave the client, whether cache_control is ever sent, what
max_tokens the main loop carries. Credential-safe by construction: the header
allowlist excludes the credential rather than filtering it.
"""

from __future__ import annotations

import json
import os
import threading

from .state import _atomic_write_json, log

SHAPES = os.path.expanduser("~/.config/claude-muse/shapes.json")

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

    # output_config is where the reasoning tier actually travels: measured
    # 2026-09-19, the endpoint takes output_config.effort in low/medium/high/
    # xhigh/max and rejects anything else, while top-level `effort` and
    # `reasoning_effort` are both unknown parameters. Without this bit the
    # census cannot answer whether CLAUDE_CODE_EFFORT_LEVEL reaches the wire.
    # Scalars only, so a schema or a stop-sequence string never lands in a file.
    output_config = payload.get("output_config")
    if isinstance(output_config, dict):
        shown = []
        for key in sorted(output_config):
            value = output_config[key]
            if isinstance(value, bool) or isinstance(value, (int, float, str)):
                shown.append(f"{key}={value}")
            else:
                shown.append(key)
        bits.append("output_config=" + ",".join(shown))

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
