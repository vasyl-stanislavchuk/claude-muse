"""The HTTP layer: the Handler that relays, plus the Server and main().

Every request runs through Handler._relay: reload policy, rewrite, census,
send with learn-and-retry, pump the stream, log one line. Local endpoints
(count_tokens, health) are served without touching the upstream.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import census as census_mod
from . import reasoning, relay, state
from .census import census
from .policy import _maybe_reload_policy
from .relay import (
    SearchSources,
    _deadline_hit,
    _enter_cooldown,
    _fold_stop_reason,
    _fold_usage_holder,
    _in_cooldown,
    _is_sse,
    _is_stop,
    _note_outcome,
    _note_usage,
    _outcome_bits,
    _read_sse_prefix,
    _sse_prefix_error,
    _stop_hint,
    _take_transient_wait,
    _tally_block,
    parse_retry_after,
    wants_web_search,
)
from .rewrite import offending_fields, rewrite
from .state import _count, _request_line, estimate_tokens, log, remember

UPSTREAM = os.environ.get("CLAUDE_MUSE_UPSTREAM", "api.meta.ai")
PORT = int(os.environ.get("CLAUDE_MUSE_PROXY_PORT", "8787"))

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
    "host",
}

# Timeouts split by phase: connecting fails fast, an accepted stream may run
# long, and only a truly runaway one gets cut. Reads re-arm per recv, so the
# read timeout bounds silence between bytes, never the stream itself.
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 600


def source_hash() -> str:
    """Identity of the code actually running, so preflight can spot a stale proxy.

    Covers the entry script plus every engine module. The file list and order
    must match preflight.sh's concatenation exactly: entry first, then
    engine/*.py in byte order (LC_ALL=C sort there, sorted() here).
    """
    try:
        root = Path(__file__).parent.parent
        blob = (root / "proxy.py").read_bytes()
        for mod in sorted((root / "engine").glob("*.py")):
            blob += mod.read_bytes()
        return hashlib.sha256(blob).hexdigest()[:12]
    except OSError:
        return "unknown"


VERSION = source_hash()


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
            with state._counters_lock:
                state._counters.client_hangups_total += 1

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
        log(
            _request_line(
                rid,
                self.command,
                self.path,
                200,
                ms,
                0,
                "estimated",
                f"input_tokens={tokens} ratio={ratio:.2f} samples={samples}",
            )
        )
        _count(200)
        payload = json.dumps({"input_tokens": tokens}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def _upstream(self, body: bytes) -> tuple:
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

    def _pump_sse(
        self, upstream, sources: SearchSources, initial: bytes = b"", deadline=None
    ) -> dict:
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
                (
                    "content_block_start",
                    {"type": "content_block_start", "index": index, "content_block": block},
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": index}),
            ):
                self._chunk(f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode())
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
                        _fold_stop_reason(usage, event.get("message"))
                    elif kind == "message_delta":
                        _fold_usage_holder(usage, event)
                        _fold_stop_reason(usage, event)
                    elif kind == "content_block_start":
                        # Tally every stream, not only the web-search ones, and
                        # before injection so our own sources block is not
                        # counted as something the model produced.
                        _tally_block(usage, event.get("content_block") or {})
                        if sources is not None:
                            sources.observe(event.get("content_block") or {}, event.get("index"))
                if (
                    sources is not None
                    and kind in ("message_delta", "message_stop")
                    and not injected
                ):
                    inject()
                flush_group()
            try:
                chunk = upstream.read(8192)
            except OSError as exc:
                # Headers are already committed, so there is nothing to retry
                # into. Serve what arrived and say why it is short.
                log(f"upstream-cut {type(exc).__name__}: {exc}")
                break
            if not chunk:
                break
            buf += chunk
        if buf:
            group.append(buf)
        if sources is not None and not injected:
            inject()
        flush_group()
        return usage

    def _pump_json(self, upstream, sources: SearchSources, deadline=None) -> dict:
        """Buffer one JSON body, observe and inject, serve it whole.

        Responses are output-bounded, so holding one is cheap; requests are
        context-bounded, which is why the census never re-serializes them.
        `sources` may be None, which skips injection and only observes.
        """
        raw = bytearray()
        while True:
            if _deadline_hit(deadline):
                break
            try:
                chunk = upstream.read(65536)
            except OSError as exc:
                log(f"upstream-cut {type(exc).__name__}: {exc}")
                break
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
        _fold_stop_reason(usage, payload)
        # Tally before injection, so the sources block the proxy appends below
        # never counts as something the model produced.
        for index, block in enumerate(payload.get("content") or []):
            if isinstance(block, dict):
                _tally_block(usage, block)
                if sources is not None:
                    sources.observe(block, index)
        if sources is not None:
            block = sources.block()
            if block is not None:
                payload.setdefault("content", []).append(block)
                log(f"injected {len(block['content'])} web search sources")
                raw = json.dumps(payload).encode()
        self._chunk(raw)
        return usage

    def _health(self) -> None:
        with state._counters_lock:
            by_status = dict(state._counters.by_status)
            requests_total = state._counters.requests_total
            transient_retries = state._counters.transient_retries_total
            learned_hits = state._counters.learned_hits_total
            usage_totals = dict(state._usage_totals)
            hangups = state._counters.client_hangups_total
            empty_200s = state._counters.empty_content_200s
            no_reasoning = state._counters.no_reasoning_requests_total
            stop_reasons = dict(state._counters.stop_reasons)
        payload = json.dumps(
            {
                "ok": True,
                "version": VERSION,
                "pid": os.getpid(),
                "upstream": UPSTREAM,
                "port": PORT,
                "uptime_s": int(time.monotonic() - state._STARTED),
                "requests": requests_total,
                "by_status": by_status,
                "transient_retries": transient_retries,
                "learned_hits": learned_hits,
                "client_hangups": hangups,
                "empty_content_200s": empty_200s,
                "no_reasoning_requests": no_reasoning,
                "stop_reasons": stop_reasons,
                "usage": usage_totals,
                "cooldown_until": relay._cooldown_until,
                "min_max_tokens": reasoning.MIN_MAX_TOKENS,
                "learned": sorted(state._learned),
                "shapes": len(census_mod._shapes),
            }
        ).encode()
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
        rid = f"r{next(state._request_ids)}"
        start = time.monotonic()
        deadline = start + relay.STREAM_TIMEOUT
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
        request_model = (
            original_payload.get("model") if isinstance(original_payload, dict) else None
        )
        request_tools = (
            bool(original_payload.get("tools")) if isinstance(original_payload, dict) else False
        )
        # Named from the wire, not from the inference. This shape is Claude
        # Code's auto-mode classifier, but all the proxy can see is that nothing
        # in the request asked for a reasoning tier, so the endpoint picks one.
        no_reasoning = (
            isinstance(original_payload, dict)
            and "thinking" not in original_payload
            and "effort" not in original_payload
            and not isinstance(original_payload.get("output_config"), dict)
        )
        if no_reasoning:
            with state._counters_lock:
                state._counters.no_reasoning_requests_total += 1

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
            log(
                _request_line(
                    rid, self.command, self.path, status, ms, attempts, full_note(), stopped
                )
            )
            _count(status)
            self._serve_buffered(status, raw_detail, ctype)

        for _ in range(relay.MAX_REPAIR_ATTEMPTS):
            attempts += 1
            if _in_cooldown():
                ms = int((time.monotonic() - start) * 1000)
                log(_request_line(rid, self.command, self.path, 503, ms, attempts, "cooldown"))
                _count(503)
                self._send_error(
                    503,
                    "claude-muse proxy: upstream cooling down "
                    "after repeated failures; retry shortly",
                )
                return
            try:
                conn, upstream = self._upstream(body)
            except OSError as exc:
                if transient_waits < relay.MAX_TRANSIENT_WAITS:
                    _take_transient_wait(waits, transient_waits, "conn")
                    transient_waits += 1
                    continue
                _enter_cooldown()
                ms = int((time.monotonic() - start) * 1000)
                log(
                    f"{rid} {self.command} {self.path} upstream-error "
                    f"{ms}ms attempts={attempts} [{full_note()}] {exc}"
                )
                _count(502)
                self._send_error(502, f"claude-muse proxy: {exc}")
                return
            if upstream.status in relay.TRANSIENT_STATUSES:
                retry_after = upstream.getheader("Retry-After")
                ctype = upstream.getheader("Content-Type") or "application/json"
                raw_detail = upstream.read(relay.ERROR_BODY_LIMIT)
                detail = raw_detail.decode("utf8", "replace")
                conn.close()
                if _is_stop(upstream.status, detail):
                    stop_and_serve(upstream.status, raw_detail, ctype)
                    return
                if transient_waits < relay.MAX_TRANSIENT_WAITS:
                    _take_transient_wait(
                        waits, transient_waits, str(upstream.status), parse_retry_after(retry_after)
                    )
                    transient_waits += 1
                    continue
                _enter_cooldown()
                ms = int((time.monotonic() - start) * 1000)
                log(
                    _request_line(
                        rid,
                        self.command,
                        self.path,
                        upstream.status,
                        ms,
                        attempts,
                        full_note(),
                        detail.strip(),
                    )
                )
                _count(upstream.status)
                self._serve_buffered(upstream.status, raw_detail, ctype)
                return
            if upstream.status != 400 or not rewritable:
                if _is_sse(upstream):
                    prefix = _read_sse_prefix(conn, upstream)
                    sse_pending = prefix
                    is_error, snippet = _sse_prefix_error(prefix)
                    retryable = (
                        is_error
                        and not _is_stop(upstream.status, snippet)
                        and relay._TRANSIENT_HINTS.search(snippet)
                    )
                    if retryable and transient_waits < relay.MAX_TRANSIENT_WAITS:
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
            raw_detail = upstream.read(relay.ERROR_BODY_LIMIT)
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
                log(
                    _request_line(
                        rid,
                        self.command,
                        self.path,
                        400,
                        ms,
                        attempts,
                        f"unnamed {full_note()}",
                        detail.strip(),
                    )
                )
                _count(400)
                self._send_error(
                    400, detail.strip() or "claude-muse proxy: upstream rejected the request"
                )
                return
            retried, extra = rewrite(original, drop_tool_choice=drop_choice)
            if retried == body:
                ms = int((time.monotonic() - start) * 1000)
                log(
                    _request_line(
                        rid,
                        self.command,
                        self.path,
                        400,
                        ms,
                        attempts,
                        f"unrecoverable {full_note()}",
                        detail.strip(),
                    )
                )
                _count(400)
                self._send_error(
                    400, detail.strip() or "claude-muse proxy: upstream rejected the request"
                )
                return
            body, notes = retried, extra
        else:
            # The budget ran out with the last rewrite never sent. Say so, rather
            # than falling through to report a 400 whose body was already consumed
            # off a connection that is now closed.
            ms = int((time.monotonic() - start) * 1000)
            log(
                _request_line(
                    rid,
                    self.command,
                    self.path,
                    400,
                    ms,
                    attempts,
                    f"attempts-exhausted {full_note()}",
                    detail.strip(),
                )
            )
            _count(400)
            self._send_error(400, detail.strip() or "claude-muse proxy: repair budget exhausted")
            return

        failed = upstream.status >= 300

        sources = SearchSources() if rewritable and not failed and wants_web_search(body) else None

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
            log(
                f"{rid} {self.command} {self.path} {upstream.status} "
                f"{ms}ms attempts={attempts} client-hung-up [{full_note()}]"
            )
            _count(upstream.status)
            with state._counters_lock:
                state._counters.client_hangups_total += 1
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
            f"{short}={usage[key]}"
            for key, short in (
                ("input_tokens", "in"),
                ("output_tokens", "out"),
                ("thinking_tokens", "think"),
            )
            if isinstance(usage.get(key), int)
        )
        usage_bits = " ".join(x for x in (usage_bits, _outcome_bits(usage, no_reasoning)) if x)
        _note_outcome(usage, upstream.status, failed)
        if failed:
            detail = bytes(captured).decode("utf8", "replace").strip()
            log(
                _request_line(
                    rid, self.command, self.path, upstream.status, ms, attempts, full_note(), detail
                )
            )
        else:
            log(
                _request_line(
                    rid,
                    self.command,
                    self.path,
                    upstream.status,
                    ms,
                    attempts,
                    full_note(),
                    usage_bits,
                )
            )

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
