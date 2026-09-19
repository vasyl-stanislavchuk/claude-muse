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
import json
import os
import re
import sys
import hashlib
import threading
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
MIN_THINKING_BUDGET = 1024

# One generation is enough to answer "what just happened"; the file used to grow
# without bound and was duplicated into proxy.err on top of that.
LOG_MAX_BYTES = 1 << 20

# 4096 truncated long errors mid-sentence, so a field named late was unlearnable.
ERROR_BODY_LIMIT = 65536
MAX_REPAIR_ATTEMPTS = 6

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


# Fields the endpoint has rejected by name at some point. Seeded with the ones
# already measured; the retry loop adds any others it meets, so a new gap costs
# one slow request instead of a debugging session.
SEEDED_DROPS = ["stop_sequences", "safeguards"]
_learned_lock = threading.Lock()


def load_learned() -> set[str]:
    try:
        with open(LEARNED) as fh:
            return set(json.load(fh)) | set(SEEDED_DROPS)
    except (OSError, ValueError):
        return set(SEEDED_DROPS)


_learned = load_learned()


def remember(field: str) -> None:
    with _learned_lock:
        if field in _learned:
            return
        _learned.add(field)
        try:
            with open(LEARNED, "w") as fh:
                json.dump(sorted(_learned), fh, indent=2)
        except OSError:
            pass
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
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body, []
    if not isinstance(payload, dict):
        return body, []

    notes: list[str] = []

    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if not str(tool.get("type", "")).startswith("web_search_"):
            continue
        for field in ("max_uses", "allowed_domains", "blocked_domains"):
            if field in tool:
                del tool[field]
                notes.append(f"-{field}")

    for field in sorted(_learned):
        if drop_field(payload, field):
            notes.append(f"-{field}")

    # Only the named form is measured as rejected: `named 'tool_choice' is not
    # supported`. The old blanket rule also caught {"type":"none"}, turning "must
    # not call tools" into "may call tools" — the proxy granting permission rather
    # than repairing a shape. `any` and `none` now pass through; if either is in
    # fact rejected, the retry path degrades it by dropping the key, because
    # absence asserts nothing where auto asserts something.
    choice = payload.get("tool_choice")
    if drop_tool_choice and "tool_choice" in payload:
        del payload["tool_choice"]
        notes.append("-tool_choice")
    elif isinstance(choice, dict) and choice.get("type") == "tool":
        payload["tool_choice"] = {"type": "auto"}
        notes.append("tool_choice tool->auto")

    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "disabled":
        del payload["thinking"]
        notes.append("thinking disabled->omitted")

    requested = payload.get("max_tokens")
    if isinstance(requested, int) and requested < MIN_MAX_TOKENS:
        payload["max_tokens"] = MIN_MAX_TOKENS
        notes.append(f"max_tokens {requested}->{MIN_MAX_TOKENS}")

    thinking = payload.get("thinking")
    ceiling = payload.get("max_tokens")
    if isinstance(thinking, dict) and isinstance(ceiling, int):
        budget = thinking.get("budget_tokens")
        if isinstance(budget, int) and budget >= ceiling:
            clamped = max(MIN_THINKING_BUDGET, ceiling - MIN_THINKING_BUDGET)
            thinking["budget_tokens"] = clamped
            notes.append(f"budget_tokens {budget}->{clamped}")

    if not notes:
        return body, []
    return json.dumps(payload).encode(), notes



# Header names the census may record. An allowlist, not a denylist, so the
# credential is excluded by construction rather than by remembering to filter it.
CENSUS_HEADERS = ("anthropic-beta", "anthropic-version", "accept")

_shapes_lock = threading.Lock()


def load_shapes() -> set[str]:
    try:
        with open(SHAPES) as fh:
            return set(json.load(fh))
    except (OSError, ValueError):
        return set()


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
        _shapes.add(sig)
        try:
            with open(SHAPES, "w") as fh:
                json.dump(sorted(_shapes), fh, indent=2)
        except OSError:
            pass
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

    def _upstream(self, body: bytes):
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        # Compression would have to be undone before it could be re-chunked, and the
        # stream matters more than the bytes saved.
        headers["Accept-Encoding"] = "identity"
        if body:
            headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPSConnection(UPSTREAM, timeout=600)
        conn.request(self.command, self.path, body=body or None, headers=headers)
        return conn, conn.getresponse()

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _pump_sse(self, upstream, sources: "SearchSources") -> None:
        """Pass the stream through event by event, then append the sources block.

        Events are emitted whole rather than byte by byte, so the injected block can
        go in ahead of the terminating message_delta without splitting an event. The
        blank line that ends each event arrives with it, so nothing is held back.
        """
        buf = b""
        group: list[bytes] = []
        injected = False

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
            chunk = upstream.read(8192)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                group.append(line + b"\n")
                if line.strip():
                    continue
                # Blank line: the event is complete.
                kind = None
                for raw in group:
                    if not raw.startswith(b"data: "):
                        continue
                    try:
                        event = json.loads(raw[6:])
                    except ValueError:
                        continue
                    kind = event.get("type")
                    if kind == "content_block_start":
                        sources.observe(event.get("content_block") or {}, event.get("index"))
                if kind in ("message_delta", "message_stop") and not injected:
                    inject()
                flush_group()
        if buf:
            group.append(buf)
        if not injected:
            inject()
        flush_group()

    def _pump_json(self, upstream, sources: "SearchSources") -> None:
        raw = upstream.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            self._chunk(raw)
            return
        for index, block in enumerate(payload.get("content") or []):
            if isinstance(block, dict):
                sources.observe(block, index)
        block = sources.block()
        if block is not None:
            payload.setdefault("content", []).append(block)
            log(f"injected {len(block['content'])} web search sources")
            raw = json.dumps(payload).encode()
        self._chunk(raw)

    def _health(self) -> None:
        payload = json.dumps({
            "ok": True,
            "version": VERSION,
            "pid": os.getpid(),
            "upstream": UPSTREAM,
            "port": PORT,
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
        if self.path.split("?")[0] == "/__health":
            self._health()
            return
        length = int(self.headers.get("Content-Length") or 0)
        original = self.rfile.read(length) if length else b""
        rewritable = self.command == "POST" and self.path.startswith("/v1/messages")

        body, notes = rewrite(original) if rewritable else (original, [])

        if rewritable:
            try:
                census(json.loads(original), self.headers, original)
            except (ValueError, UnicodeDecodeError):
                pass

        # A 400 that names a field is the endpoint teaching us its subset. Learn it,
        # strip it, retry. Nothing has been written to the client yet, so the retry
        # is invisible; the cost is one slow request the first time a gap appears.
        drop_choice = False
        detail = ""
        for _ in range(MAX_REPAIR_ATTEMPTS):
            try:
                conn, upstream = self._upstream(body)
            except OSError as exc:
                log(f"{self.command} {self.path} upstream-error {exc}")
                self._send_error(502, f"claude-muse proxy: {exc}")
                return
            if upstream.status != 400 or not rewritable:
                break
            detail = upstream.read(ERROR_BODY_LIMIT).decode("utf8", "replace")
            conn.close()
            fields = offending_fields(detail)
            if fields:
                for field in fields:
                    remember(field)
            elif "tool_choice" in detail and not drop_choice:
                # offending_fields skips tool_choice, so a 400 naming only it would
                # dead-end as "unnamed". Drop the key rather than forcing auto.
                drop_choice = True
            else:
                log(f"{self.command} {self.path} 400 unnamed [{' '.join(notes)}] {detail.strip()}")
                self._send_error(400, detail.strip() or "claude-muse proxy: upstream rejected the request")
                return
            retried, extra = rewrite(original, drop_tool_choice=drop_choice)
            if retried == body:
                log(f"{self.command} {self.path} 400 unrecoverable [{' '.join(notes)}] {detail.strip()}")
                self._send_error(400, detail.strip() or "claude-muse proxy: upstream rejected the request")
                return
            body, notes = retried, extra
        else:
            # The budget ran out with the last rewrite never sent. Say so, rather
            # than falling through to report a 400 whose body was already consumed
            # off a connection that is now closed.
            log(f"{self.command} {self.path} 400 attempts-exhausted [{' '.join(notes)}] {detail.strip()}")
            self._send_error(400, detail.strip() or "claude-muse proxy: repair budget exhausted")
            return

        note = " ".join(notes) if notes else "-"
        failed = upstream.status >= 300

        sources = SearchSources() if (rewritable and not failed and wants_web_search(body)) else None

        captured = bytearray()
        try:
            # Inside the try: the client can vanish between the upstream response
            # and these headers, which is the other two tracebacks in proxy.err.
            self.send_response(upstream.status)
            for key, value in upstream.getheaders():
                if key.lower() not in HOP_BY_HOP:
                    self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if sources is not None and "text/event-stream" in (upstream.getheader("Content-Type") or ""):
                self._pump_sse(upstream, sources)
            elif sources is not None:
                self._pump_json(upstream, sources)
            else:
                while True:
                    chunk = upstream.read(8192)
                    if not chunk:
                        break
                    if failed and len(captured) < 2048:
                        captured.extend(chunk)
                    self._chunk(chunk)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log(f"{self.command} {self.path} {upstream.status} client-hung-up [{note}]")
            conn.close()
            return
        finally:
            conn.close()

        if failed and self.path != "/api/hello":
            # /api/hello is Claude Code's reachability ping; api.meta.ai has never
            # served it, so its 404 is background noise rather than a finding.
            detail = bytes(captured).decode("utf8", "replace").strip()
            log(f"{self.command} {self.path} {upstream.status} [{note}] {detail}")
        elif notes:
            log(f"{self.command} {self.path} {upstream.status} [{note}]")

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
