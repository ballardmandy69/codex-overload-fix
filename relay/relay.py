#!/usr/bin/env python3
"""Local retry relay for Codex /responses.

Listens on http://127.0.0.1:<port> and forwards to
https://chatgpt.com/backend-api/codex, retrying when the upstream answers
with a server-overload ("Selected model is at capacity") response instead of
handing that failure straight to Codex.

Request bodies (including zstd-compressed ones), auth headers, and committed
SSE payloads are forwarded untouched. Hop-by-hop headers are normalized, and
only the stream head is inspected before the request is committed.
"""

import argparse
import http.client
import json
import math
import os
import random
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from urllib.parse import urlsplit

HOP_BY_HOP = {
    "connection",
    "proxy-connection",
    "proxy-authorization",
    "keep-alive",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "accept-encoding",
}

OVERLOAD_CODES = {
    "server_overloaded",      # response.failed / response.error
    "server_is_overloaded",   # bare {"type":"error"} event (observed on real traffic)
    "service_unavailable_error",
    "flex_unavailable",
    "queue_full",
}

CLOUDFLARE_403_MAX_ATTEMPTS = 3
TRANSPORT_502_MAX_ATTEMPTS = 3
PRECOMMIT_MAX_BYTES = 4 << 20
SSE_SNIFF_MAX_BYTES = 8192
STREAM_INSPECTION_MAX_BYTES = 256 << 10
REQUEST_METADATA_MAX_BYTES = 65536
REQUEST_METADATA_BODY_MAX_BYTES = 8 << 20
REQUEST_KINDS = frozenset({"turn", "prewarm", "compaction", "memory"})
TERMINAL_EVENTS = frozenset({
    "response.completed", "response.failed", "response.incomplete", "response.error", "error",
})
CLOUDFLARE_MARKERS = (
    b"blocked-icon",
    b"cf-ray",
    b"cloudflare",
    b"just a moment",
    b"access denied",
)
OVERLOAD_TEXT_MARKERS = (
    b"selected model is at capacity",
    b"server_is_overloaded",
    b"server_overloaded",
)


def is_overload(err):
    if isinstance(err, str):
        text = err.lower()
        return "at capacity" in text or "overload" in text
    if not isinstance(err, dict):
        return False
    code = str(err.get("code") or "").lower()
    etype = str(err.get("type") or "").lower()
    reason = str(err.get("reason") or "").lower()
    msg = str(err.get("message") or "").lower()
    return (
        code in OVERLOAD_CODES
        or etype in OVERLOAD_CODES
        or reason in OVERLOAD_CODES
        or "at capacity" in msg
        or "overloaded" in msg
        or "overload" in reason
    )


def is_cloudflare_block(status, headers, body):
    """Recognize a transient Cloudflare HTML block without retrying generic 403 responses."""
    if status != 403 or not isinstance(body, bytes):
        return False
    content_type = next(
        (str(value).lower() for key, value in headers.items() if key.lower() == "content-type"),
        "",
    )
    html = "text/html" in content_type or b"<html" in body[:4096].lower()
    if not html:
        return False
    header_text = " ".join(f"{key}:{value}" for key, value in headers.items()).lower().encode()
    haystack = body.lower() + header_text
    return any(marker in haystack for marker in CLOUDFLARE_MARKERS)


def contains_overload_text(payload):
    """Detect the user-visible capacity failure before it reaches Codex."""
    if not isinstance(payload, bytes):
        return False
    lower = payload.lower()
    return any(marker in lower for marker in OVERLOAD_TEXT_MARKERS)


def sniff_sse(payload):
    """Recognize an SSE prefix, allowing more bytes for a split field or UTF-8 BOM."""
    bom = b"\xef\xbb\xbf"
    if bom.startswith(payload):
        return None
    if payload.startswith(bom):
        payload = payload[len(bom):]
    head = payload.lstrip(b"\r\n")
    if not head:
        return None
    fields = (b":", b"data:", b"event:", b"id:", b"retry:")
    if head.startswith(fields):
        return True
    if any(field.startswith(head) for field in fields):
        return None
    return False


LOG_LOCK = threading.Lock()
LOG_SIZE = [0]
LOG_MAX = 8 * 1024 * 1024


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    with LOG_LOCK:
        print(line, flush=True)
        if ARGS.log:
            try:
                if LOG_SIZE[0] > LOG_MAX:
                    os.replace(ARGS.log, ARGS.log + ".1")
                    LOG_SIZE[0] = 0
                with open(ARGS.log, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                LOG_SIZE[0] += len(line) + 1
            except OSError:
                pass


ARGS = None
REQUEST_SEM = None


class RequestTooLarge(ValueError):
    """Local request size limit exceeded.

    @author lyt
    @date 2026-10-24
    """


def upstream_connection(timeout):
    host = ARGS.upstream_host
    port = ARGS.upstream_port
    if ARGS.proxy:
        phost, pport = ARGS.proxy
        # HTTPSConnection sends plaintext CONNECT to the HTTP proxy, THEN
        # wraps the tunnel in TLS verified against the upstream hostname.
        conn_cls = (http.client.HTTPSConnection
                    if ARGS.upstream_scheme == "https"
                    else http.client.HTTPConnection)
        conn = conn_cls(phost, pport, timeout=timeout)
        conn.set_tunnel(host, port)
        return conn
    if ARGS.upstream_scheme == "http":
        return http.client.HTTPConnection(host, port, timeout=timeout)
    return http.client.HTTPSConnection(host, port, timeout=timeout)


def close_upstream(conn):
    """Release a retained HTTP response, including connection-close bodies."""
    try:
        response = getattr(conn, "_relay_response", None)
        if response is not None:
            response.close()
    finally:
        conn.close()


def read_request_body(handler, max_bytes):
    """Bound framing and payload memory before forwarding any upstream bytes."""
    lengths = handler.headers.get_all("Content-Length", [])
    te = (handler.headers.get("Transfer-Encoding") or "").lower().strip()
    if len(lengths) > 1 or (te and lengths):
        raise ValueError("ambiguous request framing")
    if te and te != "chunked":
        raise ValueError("unsupported Transfer-Encoding")
    if te:
        chunks, total = [], 0
        while True:
            line = handler.rfile.readline(8193)
            if len(line) > 8192 or not line.endswith(b"\r\n"):
                raise ValueError("invalid chunk header")
            token = line.split(b";", 1)[0].strip()
            if not token or any(c not in b"0123456789abcdefABCDEF" for c in token):
                raise ValueError("invalid chunk size")
            size = int(token, 16)
            if not size:
                trailer_size = 0
                while True:
                    trailer = handler.rfile.readline(8193)
                    trailer_size += len(trailer)
                    if trailer_size > 8192 or not trailer.endswith(b"\r\n"):
                        raise ValueError("invalid chunk trailer")
                    if trailer == b"\r\n":
                        return b"".join(chunks)
            total += size
            if total > max_bytes:
                raise RequestTooLarge
            chunk = handler.rfile.read(size)
            if len(chunk) != size or handler.rfile.read(2) != b"\r\n":
                raise ValueError("truncated chunked request")
            chunks.append(chunk)
    token = lengths[0].strip() if lengths else "0"
    if not token.isascii() or not token.isdecimal():
        raise ValueError("invalid Content-Length")
    length = int(token)
    if length > max_bytes:
        raise RequestTooLarge
    body = handler.rfile.read(length)
    if len(body) != length:
        raise ValueError("truncated request body")
    return body


def forward_headers(handler):
    out = {}
    connection_tokens = {v.strip().lower() for v in handler.headers.get("Connection", "").split(",")}
    for k, v in handler.headers.items():
        if k.lower() in HOP_BY_HOP | connection_tokens:
            continue
        out[k] = v
    host = ARGS.upstream_host
    if ":" in host:
        host = "[" + host + "]"
    if ARGS.upstream_port != (443 if ARGS.upstream_scheme == "https" else 80):
        host = f"{host}:{ARGS.upstream_port}"
    out["Host"] = host
    out["Accept-Encoding"] = "identity"
    return out


def _metadata_request_kind(value):
    if not isinstance(value, str) or len(value) > REQUEST_METADATA_MAX_BYTES:
        return None
    try:
        metadata = json.loads(value)
    except (ValueError, TypeError, RecursionError):
        return None
    kind = metadata.get("request_kind") if isinstance(metadata, dict) else None
    return kind if isinstance(kind, str) and kind in REQUEST_KINDS else None


def classify_request(path, headers, body):
    """Read explicit Codex request metadata without matching prompts or decompressing bodies."""
    if path.split("?", 1)[0].rstrip("/").endswith("/responses/compact"):
        return "compaction", "endpoint"
    metadata = next((value for key, value in headers.items()
                     if key.lower() == "x-codex-turn-metadata"), None)
    kind = _metadata_request_kind(metadata)
    if kind is not None:
        return kind, "header"
    encoding = next((value for key, value in headers.items()
                     if key.lower() == "content-encoding"), "identity")
    if encoding.strip().lower() not in ("", "identity") or len(body) > REQUEST_METADATA_BODY_MAX_BYTES:
        return "unknown", "unavailable"
    try:
        request = json.loads(body)
    except (ValueError, TypeError, RecursionError):
        return "unknown", "unavailable"
    metadata = request.get("client_metadata") if isinstance(request, dict) else None
    if isinstance(metadata, dict):
        kind = _metadata_request_kind(metadata.get("x-codex-turn-metadata"))
        if kind is not None:
            return kind, "client_metadata"
    return "unknown", "unavailable"


def has_output_content(value):
    """Distinguish known empty output containers from substantive or unknown output."""
    if not isinstance(value, dict):
        return bool(value)
    if value.get("type") not in (
        None, "message", "reasoning", "function_call", "custom_tool_call",
        "output_text", "refusal", "reasoning_text", "summary_text",
    ):
        return True
    if any(value.get(field) for field in ("text", "refusal", "arguments", "input")):
        return True
    for field in ("content", "summary"):
        parts = value.get(field) or []
        if not isinstance(parts, list):
            return True
        for part in parts:
            if not isinstance(part, dict) or part.get("text") or part.get("refusal"):
                return True
    return False


def classify_event(payload):
    """Retry explicit overload only; unknown/malformed events commit conservatively."""
    try:
        evt = json.loads(payload)
    except (ValueError, TypeError, RecursionError):
        return "commit", "unrecognized event"
    return _classify_event_document(evt)


def _classify_event_document(evt):
    """Classify an already parsed event for retry decisions and progress tracking."""
    if not isinstance(evt, dict):
        return "commit", "unrecognized event"
    kind = evt.get("type")
    response = evt.get("response")
    response = response if isinstance(response, dict) else {}
    if kind in ("response.failed", "response.error", "error"):
        candidates = (
            response.get("error"),
            evt.get("error"),
            response.get("status_details"),
            evt.get("status_details"),
            evt,
        )
        err = next((candidate for candidate in candidates if is_overload(candidate)), None)
        if err is not None:
            if isinstance(err, dict):
                code = str(err.get("code") or err.get("type") or err.get("reason") or "unknown")
            else:
                code = "server_overloaded"
            return "retry", "overload:" + code
        return "commit", f"non-retryable error:{kind}"
    if kind in ("response.created", "response.in_progress", "response.queued", "response.metadata"):
        output = response.get("output")
        if output and (not isinstance(output, list) or any(has_output_content(item) for item in output)):
            return "commit", f"lifecycle output:{kind}"
        return None, None
    if kind in ("response.output_item.added", "response.output_item.done"):
        item = evt.get("item") or {}
        if (kind.endswith(".done") and isinstance(item, dict)
                and item.get("type") in ("function_call", "custom_tool_call")):
            return "commit", f"completed tool call:{kind}"
        if has_output_content(item):
            return "commit", f"output item content:{kind}"
        return None, None
    if kind in (
        "response.content_part.added", "response.content_part.done",
        "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
    ):
        if has_output_content(evt.get("part") or {}):
            return "commit", f"output part content:{kind}"
        return None, None
    if kind in (
        "response.output_text.delta", "response.reasoning_text.delta",
        "response.reasoning_summary_text.delta", "response.function_call_arguments.delta",
        "response.custom_tool_call_input.delta", "response.refusal.delta",
    ) and not evt.get("delta"):
        return None, None
    if kind in ("ping", "heartbeat"):
        return None, None
    return "commit", f"output or unknown event:{kind}"


class SSEParser:
    """Incremental SSE framing with multiline data and CR/LF support.

    @author lyt
    @date 2026-11-14
    """

    def __init__(self):
        self.line = bytearray()
        self.data = []
        self.after_cr = False
        self.first_line = True

    def feed(self, chunk):
        for char in chunk:
            if self.after_cr:
                self.after_cr = False
                if char == 10:
                    continue
            if char in (10, 13):
                line = bytes(self.line)
                self.line.clear()
                if self.first_line:
                    line = line.removeprefix(b"\xef\xbb\xbf")
                    self.first_line = False
                self.after_cr = char == 13
                if not line:
                    if self.data:
                        payload = b"\n".join(self.data)
                        self.data.clear()
                        yield payload
                elif line.startswith(b"data:"):
                    value = line[5:]
                    self.data.append(value[1:] if value.startswith(b" ") else value)
            else:
                self.line.append(char)


class StreamObserver:
    """Inspect bounded SSE metadata without retaining response text in diagnostics.

    @author lyt
    @date 2026-11-14
    """

    def __init__(self, is_sse):
        self.parser = SSEParser() if is_sse else None
        self.terminal = "none"
        self.inspection = "sse" if is_sse else "opaque"

    def feed(self, chunk):
        if self.parser is None:
            return True
        progress = False
        for offset in range(0, len(chunk), 65536):
            for payload in self.parser.feed(chunk[offset:offset + 65536]):
                if len(payload) > STREAM_INSPECTION_MAX_BYTES:
                    self._stop_inspection()
                    return True
                try:
                    evt = json.loads(payload)
                except (ValueError, TypeError, RecursionError):
                    progress = True
                    continue
                if _classify_event_document(evt)[0] is not None:
                    progress = True
                kind = evt.get("type") if isinstance(evt, dict) else None
                if isinstance(kind, str) and kind in TERMINAL_EVENTS:
                    self.terminal = kind
            pending = len(self.parser.line) + sum(map(len, self.parser.data))
            if pending > STREAM_INSPECTION_MAX_BYTES:
                self._stop_inspection()
                return True
        return progress

    def _stop_inspection(self):
        self.parser = None
        self.inspection = "stopped"


class ConnectionWatch:
    """Interrupt blocked upstream I/O on cancellation or the current phase's deadline.

    @author lyt
    @date 2026-11-14
    """

    def __init__(self, conn, cancelled, deadline, timeout_reason="precommit_deadline", request_id=None):
        self.conn = conn
        self.cancelled = cancelled
        self.deadline = deadline
        self.timeout_reason = timeout_reason
        self.request_id = request_id
        self.reason = None
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stopped.set()
        self.thread.join()

    def check_interrupted(self):
        if self.reason is not None:
            raise OSError(f"relay stopped upstream I/O ({self.reason})")

    def run(self):
        while not self.stopped.wait(0.1):
            if self.cancelled():
                self.reason = "client_cancelled"
            elif time.monotonic() >= self.deadline:
                self.reason = self.timeout_reason
            else:
                continue
            prefix = f"[{self.request_id}] " if self.request_id is not None else ""
            log(f"{prefix}relay closing upstream: reason={self.reason}")
            sock = getattr(self.conn, "_relay_socket", None) or self.conn.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                # Windows' timed recv can remain in select after shutdown.
                # Detach before closing the handle to prevent a later double close.
                try:
                    handle = sock.detach()
                    if handle != -1:
                        socket.close(handle)
                except OSError:
                    pass
            return


def attempt(path, headers, body, timeout, commit_by, cancelled=lambda: False, request_id=None):
    """Return mode/status/headers/body/connection/response for one attempt.

    Retry recognized response failures before committing any output.
    Transport failures use the caller's bounded retry policy.
    """
    conn = upstream_connection(timeout)
    watch = ConnectionWatch(conn, cancelled, commit_by, request_id=request_id)
    retained = False
    try:
        with watch:
            conn.request("POST", path, body=body, headers=headers)
            conn._relay_socket = conn.sock
            resp = conn.getresponse()
            conn._relay_response = resp
            response_headers = dict(resp.getheaders())
            if resp.status != 200:
                data = resp.read(1048577)
                watch.check_interrupted()
                if len(data) > 1048576:
                    return "passthrough", 502, {}, b"relay: upstream error too large\n", None, None
                if resp.status in (429, 500, 502, 503, 504):
                    return "retry", resp.status, response_headers, data, None, f"http {resp.status}"
                if is_cloudflare_block(resp.status, response_headers, data):
                    return "retry", resp.status, response_headers, data, None, "cloudflare_403"
                return "passthrough", resp.status, response_headers, data, None, None
            content_type = resp.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            encoding = resp.getheader("Content-Encoding", "identity").strip().lower()
            if encoding not in ("", "identity"):
                # Encoded bytes cannot be classified without decoding; forward them unchanged.
                prelude = resp.read1(65536)
                watch.check_interrupted()
                log(f"upstream 200 encoded response: encoding={encoding}; streaming unchanged")
                retained = True
                return "stream", 200, response_headers, prelude, conn, resp
            prelude = bytearray()
            if not content_type:
                while len(prelude) < SSE_SNIFF_MAX_BYTES:
                    chunk = resp.read1(SSE_SNIFF_MAX_BYTES - len(prelude))
                    watch.check_interrupted()
                    if not chunk:
                        break
                    prelude.extend(chunk)
                    inferred_sse = sniff_sse(bytes(prelude))
                    if inferred_sse is not None:
                        if inferred_sse:
                            content_type = "text/event-stream"
                            response_headers = {
                                key: value for key, value in response_headers.items()
                                if key.lower() != "content-type"
                            }
                            response_headers["Content-Type"] = content_type
                            log("upstream 200: missing Content-Type; detected SSE prefix")
                        break
            if content_type != "text/event-stream":
                prelude.extend(resp.read(PRECOMMIT_MAX_BYTES + 1 - len(prelude)))
                watch.check_interrupted()
                prelude = bytes(prelude)
                log(f"upstream 200 non-SSE: content_type={content_type or 'unknown'} "
                    f"encoding={encoding} bytes={len(prelude)}")
                if len(prelude) <= PRECOMMIT_MAX_BYTES and contains_overload_text(prelude):
                    return "retry", 200, response_headers, prelude, None, "overload_text"
                if len(prelude) <= PRECOMMIT_MAX_BYTES:
                    verdict, info = classify_event(prelude)
                    if verdict == "retry":
                        return "retry", 200, response_headers, prelude, None, info
                retained = True
                return "stream", 200, response_headers, prelude, conn, resp
            pending = bytes(prelude)
            prelude.clear()
            parser = SSEParser()
            while True:
                chunk = pending or resp.read1(65536)
                pending = b""
                watch.check_interrupted()
                if not chunk:
                    return ("retry", 502, {}, b"relay: upstream stream ended before output\n",
                            None, "transport_502")
                prelude.extend(chunk)
                for payload in parser.feed(chunk):
                    verdict, info = classify_event(payload)
                    if verdict == "retry":
                        return "retry", 200, response_headers, bytes(prelude), None, info
                    if verdict == "commit":
                        log(f"SSE commit boundary: {info}")
                        retained = True
                        return "stream", 200, response_headers, bytes(prelude), conn, resp
                if len(prelude) >= PRECOMMIT_MAX_BYTES:
                    log(f"SSE metadata before output exceeded buffer limit: {len(prelude)} bytes")
                    return ("retry", 502, {}, b"relay: too much metadata before upstream output\n",
                            None, "transport_502")
    except (OSError, http.client.HTTPException) as exc:
        reason = watch.reason
        if reason is None:
            if isinstance(exc, socket.timeout):
                reason = "precommit_deadline" if time.monotonic() >= commit_by else "socket_timeout"
            else:
                reason = "upstream_disconnect"
        prefix = f"[{request_id}] " if request_id is not None else ""
        log(f"{prefix}upstream connection interrupted: reason={reason}; {type(exc).__name__}: {exc}")
        message = (b"relay: timed out waiting for upstream output\n"
                   if reason == "precommit_deadline" else b"relay: upstream connection interrupted\n")
        return "retry", 502, {}, message, None, "transport_502"
    finally:
        if not retained:
            close_upstream(conn)


class LocalServer(ThreadingHTTPServer):
    """Bound worker threads, including clients that never finish HTTP headers.

    @author lyt
    @date 2026-10-24
    """

    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # HTTPServer.server_bind performs getfqdn(), which can block on local DNS.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def process_request(self, request, address):
        if not REQUEST_SEM.acquire(blocking=False):
            # Bound time in the accept loop even if a local client does not read.
            request.settimeout(0.1)
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Content-Length: 0\r\nRetry-After: 1\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            REQUEST_SEM.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            REQUEST_SEM.release()


class Handler(BaseHTTPRequestHandler):
    """Loopback request forwarding with a conservative retry boundary.

    @author lyt
    @date 2026-11-14
    """

    def setup(self):
        self.request.settimeout(ARGS.client_timeout)
        super().setup()

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence default stderr logging
        pass

    def _proxy(self):
        try:
            self._proxy_inner()
        except (OSError, http.client.HTTPException):
            log(f"[{self.client_address[1]}] local connection interrupted")
        finally:
            self.close_connection = True

    def _proxy_inner(self):
        if self.command == "GET" and self.path == "/health":
            self._send_simple(200, {"Content-Type": "application/json"},
                              b'{"service":"codex-overload-fix","version":"local-hardened-1"}')
            return
        if not self.path.startswith("/") or self.path.startswith("//"):
            self._send_simple(400, {}, b"relay: origin-form path required\n")
            return
        path = self.path
        upstream_path = ARGS.upstream_prefix + path
        try:
            body = read_request_body(self, ARGS.max_body_bytes)
        except RequestTooLarge:
            log(f"[{self.client_address[1]}] rejected request over {ARGS.max_body_bytes}B")
            self._send_simple(
                413,
                {"Content-Type": "text/plain; charset=utf-8"},
                b"relay: request body too large\n",
            )
            return
        except ValueError as exc:
            log(f"[{self.client_address[1]}] rejected malformed request: {exc}")
            self._send_simple(
                400,
                {"Content-Type": "text/plain; charset=utf-8"},
                b"relay: malformed request body\n",
            )
            return
        headers = forward_headers(self)

        endpoint = path.split("?", 1)[0].rstrip("/")
        is_responses = self.command == "POST" and endpoint.endswith(("/responses", "/responses/compact"))
        if not is_responses:
            self._passthrough(upstream_path, headers, body)
            return

        cport = self.client_address[1]
        started = time.monotonic()
        kind, kind_source = classify_request(path, headers, body)
        compaction = kind == "compaction"
        total_budget = ARGS.compaction_timeout if compaction else ARGS.total_budget
        first_output_timeout = ARGS.compaction_timeout if compaction else ARGS.commit_deadline
        idle_timeout = ARGS.compaction_timeout if compaction else ARGS.stream_idle_timeout
        io_timeout = max(ARGS.timeout, ARGS.compaction_timeout) if compaction else ARGS.timeout
        log(f"[{cport}] {self.command} {path} ({len(body)}B) kind={kind}; source={kind_source}; "
            f"budget={total_budget}s; first_output={first_output_timeout}s; stream_idle={idle_timeout}s")
        deadline = started + total_budget
        last = None
        attempts = 0
        transport_grace = False
        transport_failures = 0
        for attempt_no in range(1, ARGS.max_attempts + 1):
            remaining = deadline - time.monotonic()
            if self._client_gone():
                log(f"[{cport}] cancelled before attempt {attempt_no}")
                return
            if remaining <= 0:
                break
            timeout = min(io_timeout, first_output_timeout, remaining)
            attempts += 1
            mode, status, resp_headers, prelude, conn, resp = attempt(
                upstream_path, headers, body, timeout,
                time.monotonic() + timeout, self._client_gone, request_id=cport,
            )
            if mode == "stream":
                log(f"[{cport}] attempt {attempt_no}: ok, streaming; "
                    f"waited={time.monotonic() - started:.1f}s")
                self._stream_back(resp_headers, prelude, conn, resp, started=started,
                                  idle_timeout=idle_timeout, io_timeout=io_timeout)
                return
            if mode == "passthrough":
                log(f"[{cport}] attempt {attempt_no}: upstream {status}, passthrough")
                self._send_simple(status, resp_headers, prelude)
                return
            last = (status, resp_headers, prelude)
            log(f"[{cport}] attempt {attempt_no}: retryable ({resp})")
            if resp == "transport_502":
                transport_failures += 1
            else:
                transport_failures = 0
            if resp == "cloudflare_403" and attempt_no >= CLOUDFLARE_403_MAX_ATTEMPTS:
                log(f"[{cport}] Cloudflare 403 retry limit reached; passthrough")
                self._send_simple(status, resp_headers, prelude)
                return
            if resp == "transport_502" and transport_failures >= TRANSPORT_502_MAX_ATTEMPTS:
                log(f"[{cport}] transport 502 consecutive retry limit reached; passthrough")
                self._send_simple(status, resp_headers, prelude)
                return
            transport_grace = resp == "transport_502"
            if attempt_no == ARGS.max_attempts:
                break
            if transport_grace:
                delay = 0.0
                log(f"[{cport}] transport 502: immediate retry grace")
            else:
                delay = min(ARGS.backoff_max, ARGS.backoff_base * (1.6 ** min(attempt_no - 1, 100)))
                delay = min(ARGS.backoff_max, delay * random.uniform(0.7, 1.3))
            retry_after = next((v for k, v in resp_headers.items() if k.lower() == "retry-after"), "")
            if retry_after.isdecimal():
                if int(retry_after) > ARGS.backoff_max:
                    log(f"[{cport}] Retry-After exceeds fast retry limit; returning upstream failure")
                    self._send_simple(status, resp_headers, prelude)
                    return
                delay = max(delay, int(retry_after))
            wait_until = min(deadline, time.monotonic() + delay)
            while time.monotonic() < wait_until:
                if self._client_gone():
                    log(f"[{cport}] cancelled during backoff")
                    return
                time.sleep(min(0.1, max(0, wait_until - time.monotonic())))
        log(f"[{cport}] giving up after {attempts} attempts; "
            f"status={last[0] if last else 504}; elapsed={time.monotonic() - started:.1f}s")
        if last:
            self._send_simple(*last)
        else:
            self._send_simple(504, {}, b"relay: retry budget exhausted\n")

    def _client_gone(self):
        """True when the Codex side has closed its end of the socket."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _passthrough(self, path, headers, body):
        started = time.time()
        conn = upstream_connection(ARGS.timeout)
        try:
            conn.request(self.command, path, body=body or None, headers=headers)
            resp = conn.getresponse()
            data = resp.read(ARGS.max_body_bytes + 1)
            if len(data) > ARGS.max_body_bytes:
                self._send_simple(
                    502,
                    {"Content-Type": "text/plain; charset=utf-8"},
                    b"relay: upstream response body too large\n",
                )
                log(f"[{self.client_address[1]}] {self.command} {path} -> response too large")
                return
            self._send_simple(resp.status, dict(resp.getheaders()), data)
            log(f"[{self.client_address[1]}] {self.command} {path} -> {resp.status} "
                f"{len(data)}B in {time.time() - started:.1f}s")
        except Exception as exc:  # noqa: BLE001
            log(f"[{self.client_address[1]}] {self.command} {path} -> error {exc!r}")
            self._send_simple(502, {"Content-Type": "text/plain"}, f"relay: {exc}\n".encode())
        finally:
            conn.close()

    def _stream_back(self, resp_headers, prelude, conn, resp, started=None,
                     idle_timeout=None, io_timeout=None):
        stream_started = time.monotonic()
        started = stream_started if started is None else started
        idle_timeout = ARGS.stream_idle_timeout if idle_timeout is None else idle_timeout
        io_timeout = ARGS.timeout if io_timeout is None else io_timeout
        forwarded = 0
        outcome = "upstream_eof"
        content_type = next((value for key, value in resp_headers.items()
                             if key.lower() == "content-type"), "")
        encoding = next((value for key, value in resp_headers.items()
                         if key.lower() == "content-encoding"), "identity")
        is_sse = (content_type.split(";", 1)[0].strip().lower() == "text/event-stream"
                  and encoding.strip().lower() in ("", "identity"))
        observer = StreamObserver(is_sse)
        # Reparse the preserved head, including any partial following event.
        observer.feed(prelude)
        watch = ConnectionWatch(
            conn, self._client_gone, time.monotonic() + idle_timeout,
            timeout_reason="stream_idle_timeout", request_id=self.client_address[1],
        )
        try:
            with watch:
                self.send_response(200)
                for key, value in resp_headers.items():
                    if key.lower() not in HOP_BY_HOP:
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                stream_socket = getattr(conn, "_relay_socket", None) or conn.sock
                if stream_socket is not None:
                    stream_socket.settimeout(io_timeout)
                self.wfile.write(prelude)
                forwarded += len(prelude)
                self.wfile.flush()
                while True:
                    chunk = resp.read1(65536)
                    watch.check_interrupted()
                    if not chunk:
                        break
                    if observer.feed(chunk):
                        watch.deadline = time.monotonic() + idle_timeout
                    self.wfile.write(chunk)
                    forwarded += len(chunk)
                    self.wfile.flush()
        except (OSError, http.client.HTTPException) as exc:
            outcome = watch.reason or ("socket_timeout" if isinstance(exc, socket.timeout) else "stream_io_error")
        finally:
            outcome = watch.reason or outcome
            if observer.terminal == "response.completed" and outcome in ("upstream_eof", "client_cancelled"):
                outcome = "completed"
            log(f"[{self.client_address[1]}] stream ended: reason={outcome}; terminal={observer.terminal}; "
                f"forwarded={forwarded}B; elapsed={time.monotonic() - started:.1f}s; "
                f"streaming={time.monotonic() - stream_started:.1f}s; inspection={observer.inspection}; "
                "no relay replay")
            close_upstream(conn)

    def _send_simple(self, status, headers, data):
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() in HOP_BY_HOP:
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_DELETE = _proxy
    do_PATCH = _proxy


def parse_upstream(value):
    parts = urlsplit(value if "://" in value else "https://" + value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("upstream requires http(s) and a host")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise ValueError("upstream credentials/query/fragment are unsupported")
    port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise ValueError("invalid upstream port")
    return parts.scheme, parts.hostname, port, parts.path.rstrip("/")


def parse_proxy(value):
    """Accept a loopback HTTP proxy (Clash HTTP or mixed port)."""
    raw = value.strip()
    parts = urlsplit(raw if "://" in raw else "http://" + raw)
    if parts.scheme != "http":
        raise ValueError("use http:// with Clash HTTP/mixed port; SOCKS/HTTPS proxies are unsupported")
    if parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("local mode requires a loopback proxy host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("proxy authentication is unsupported")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("proxy URL cannot contain a path/query/fragment")
    port = parts.port if parts.port is not None else 80
    if not 1 <= port <= 65535:
        raise ValueError("proxy port must be between 1 and 65535")
    return "http", parts.hostname, port


def main():
    global ARGS, REQUEST_SEM
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--upstream", default="chatgpt.com/backend-api/codex")
    p.add_argument(
        "--proxy", default="",
        help="Local HTTP proxy, e.g. http://127.0.0.1:7890 (Clash mixed/HTTP)",
    )
    p.add_argument(
        "--clash-port", type=int, default=0,
        help="shortcut for http://127.0.0.1:<port>; Clash HTTP/mixed is usually 7890",
    )
    p.add_argument("--clash-host", choices=("127.0.0.1", "localhost"), default="127.0.0.1")
    p.add_argument("--max-attempts", type=int, default=10)
    p.add_argument("--total-budget", type=float, default=20.0)
    p.add_argument("--backoff-base", type=float, default=0.2)
    p.add_argument("--backoff-max", type=float, default=0.5)
    p.add_argument("--commit-deadline", type=float, default=20.0,
                   help="max seconds per attempt to wait for substantive output")
    p.add_argument("--stream-idle-timeout", type=float, default=30.0,
                   help="max seconds without output progress after committing a stream")
    p.add_argument("--compaction-timeout", type=float, default=120.0,
                   help="pre-output budget and stream idle timeout for explicitly marked compaction requests")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--max-body-bytes", type=int, default=64 << 20)
    p.add_argument("--max-concurrency", type=int, default=8)
    p.add_argument("--client-timeout", type=float, default=30.0)
    p.add_argument("--log", default="")
    ARGS = p.parse_args()

    if not 1 <= ARGS.port <= 65535 or not 1 <= ARGS.max_attempts <= 1000:
        p.error("invalid port or max-attempts (1..1000)")
    for name in ("timeout", "total_budget", "commit_deadline", "stream_idle_timeout", "compaction_timeout",
                 "client_timeout", "backoff_base", "backoff_max"):
        value = getattr(ARGS, name)
        if not math.isfinite(value) or value <= 0:
            p.error(name + " must be a finite positive number")
    if ARGS.clash_port:
        if ARGS.proxy:
            p.error("use --proxy or --clash-port, not both")
        if not 1 <= ARGS.clash_port <= 65535:
            p.error("--clash-port must be between 1 and 65535")
        ARGS.proxy = f"http://{ARGS.clash_host}:{ARGS.clash_port}"
    if ARGS.max_body_bytes <= 0 or ARGS.max_concurrency <= 0:
        p.error("--max-body-bytes and --max-concurrency must be positive")

    if ARGS.log and os.path.exists(ARGS.log):
        try:
            LOG_SIZE[0] = os.path.getsize(ARGS.log)
        except OSError:
            LOG_SIZE[0] = 0
    try:
        ARGS.upstream_scheme, ARGS.upstream_host, ARGS.upstream_port, ARGS.upstream_prefix = (
            parse_upstream(ARGS.upstream)
        )
    except ValueError as exc:
        p.error(str(exc))
    if ARGS.proxy:
        try:
            ARGS.proxy_scheme, proxy_host, proxy_port = parse_proxy(ARGS.proxy)
        except ValueError as exc:
            p.error(str(exc))
        ARGS.proxy = (proxy_host, proxy_port)
    else:
        ARGS.proxy_scheme = ""

    REQUEST_SEM = threading.BoundedSemaphore(ARGS.max_concurrency)
    proxy_desc = (
        f"{ARGS.proxy_scheme}://{ARGS.proxy[0]}:{ARGS.proxy[1]}"
        if ARGS.proxy else "direct"
    )

    log(f"relay listening on 127.0.0.1:{ARGS.port} -> {ARGS.upstream} via {proxy_desc}")
    log(f"retry policy: attempts={ARGS.max_attempts} budget={ARGS.total_budget}s "
        f"precommit={ARGS.commit_deadline}s backoff={ARGS.backoff_base}..{ARGS.backoff_max}s "
        f"stream_idle={ARGS.stream_idle_timeout}s compaction={ARGS.compaction_timeout}s "
        f"io_timeout={ARGS.timeout}s")
    srv = LocalServer(("127.0.0.1", ARGS.port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    sys.exit(main())
