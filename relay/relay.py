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


def is_overload(err):
    if not isinstance(err, dict):
        return False
    code = str(err.get("code") or "").lower()
    etype = str(err.get("type") or "").lower()
    msg = str(err.get("message") or "").lower()
    return (
        code in OVERLOAD_CODES
        or etype in OVERLOAD_CODES
        or "at capacity" in msg
        or "overloaded" in msg
    )

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


def classify_event(payload):
    """Retry explicit overload only; unknown/malformed events commit conservatively."""
    try:
        evt = json.loads(payload)
    except (ValueError, TypeError):
        return "commit", "unrecognized event"
    if not isinstance(evt, dict):
        return "commit", "unrecognized event"
    kind = evt.get("type")
    response = evt.get("response")
    response = response if isinstance(response, dict) else {}
    if kind in ("response.failed", "error"):
        err = response.get("error") if kind == "response.failed" else evt.get("error")
        if is_overload(err):
            code = str(err.get("code") or err.get("type") or "unknown")
            return "retry", "overload:" + code
        return "commit", "non-retryable error"
    if kind in ("response.created", "response.in_progress"):
        # An unexpectedly populated lifecycle event is already substantive output.
        return ("commit", "lifecycle output") if response.get("output") else (None, None)
    return "commit", "output or unknown event"


class SSEParser:
    """Incremental SSE framing with multiline data and CR/LF support.

    @author lyt
    @date 2026-10-24
    """

    def __init__(self):
        self.line = bytearray()
        self.data = []
        self.after_cr = False

    def feed(self, chunk):
        for char in chunk:
            if self.after_cr:
                self.after_cr = False
                if char == 10:
                    continue
            if char in (10, 13):
                line = bytes(self.line)
                self.line.clear()
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


class ConnectionWatch:
    """Interrupt blocked upstream I/O on cancellation or a precommit deadline.

    @author lyt
    @date 2026-10-24
    """

    def __init__(self, conn, cancelled, deadline):
        self.conn = conn
        self.cancelled = cancelled
        self.deadline = deadline
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stopped.set()
        self.thread.join()

    def run(self):
        while not self.stopped.wait(0.1):
            if self.cancelled() or time.monotonic() >= self.deadline:
                sock = getattr(self.conn, "_relay_socket", None) or self.conn.sock
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                return


def attempt(path, headers, body, timeout, commit_by, cancelled=lambda: False):
    """Return mode/status/headers/body/connection/response for one attempt.

    Only explicit overload responses retry. Transport failures have an unknown
    upstream execution outcome and are surfaced without replaying the POST.
    """
    conn = upstream_connection(timeout)
    retained = False
    try:
        with ConnectionWatch(conn, cancelled, commit_by):
            conn.request("POST", path, body=body, headers=headers)
            conn._relay_socket = conn.sock
            resp = conn.getresponse()
            response_headers = dict(resp.getheaders())
            if resp.status != 200:
                data = resp.read(1048577)
                if len(data) > 1048576:
                    return "passthrough", 502, {}, b"relay: upstream error too large\n", None, None
                if resp.status in (429, 500, 502, 503, 504):
                    return "retry", resp.status, response_headers, data, None, f"http {resp.status}"
                return "passthrough", resp.status, response_headers, data, None, None
            content_type = resp.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            encoding = resp.getheader("Content-Encoding", "identity").strip().lower()
            if content_type != "text/event-stream" or encoding not in ("", "identity"):
                retained = True
                return "stream", 200, response_headers, b"", conn, resp
            prelude = bytearray()
            parser = SSEParser()
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    return "passthrough", 502, {}, b"relay: upstream stream ended before output\n", None, None
                prelude.extend(chunk)
                for payload in parser.feed(chunk):
                    verdict, info = classify_event(payload)
                    if verdict == "retry":
                        return "retry", 200, response_headers, bytes(prelude), None, info
                    if verdict == "commit":
                        retained = True
                        return "stream", 200, response_headers, bytes(prelude), conn, resp
                if len(prelude) >= 262144:
                    retained = True
                    return "stream", 200, response_headers, bytes(prelude), conn, resp
    except (OSError, http.client.HTTPException):
        return "passthrough", 502, {}, b"relay: upstream connection interrupted; not replayed\n", None, None
    finally:
        if not retained:
            conn.close()


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
        super().server_bind()

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
    @date 2026-10-24
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

        is_responses = self.command == "POST" and path.split("?")[0].endswith("/responses")
        if not is_responses:
            self._passthrough(upstream_path, headers, body)
            return

        cport = self.client_address[1]
        log(f"[{cport}] {self.command} {path} ({len(body)}B)")
        deadline = time.monotonic() + ARGS.total_budget
        last = None
        for attempt_no in range(1, ARGS.max_attempts + 1):
            remaining = deadline - time.monotonic()
            if self._client_gone():
                log(f"[{cport}] cancelled before attempt {attempt_no}")
                return
            if remaining <= 0:
                break
            timeout = min(ARGS.timeout, ARGS.commit_deadline, remaining)
            mode, status, resp_headers, prelude, conn, resp = attempt(
                upstream_path, headers, body, timeout,
                time.monotonic() + timeout, self._client_gone,
            )
            if mode == "stream":
                log(f"[{cport}] attempt {attempt_no}: ok, streaming")
                try:
                    with ConnectionWatch(conn, self._client_gone, float("inf")):
                        self._stream_back(resp_headers, prelude, conn, resp)
                finally:
                    conn.close()
                return
            if mode == "passthrough":
                log(f"[{cport}] attempt {attempt_no}: upstream {status}, passthrough")
                self._send_simple(status, resp_headers, prelude)
                return
            last = (status, resp_headers, prelude)
            log(f"[{cport}] attempt {attempt_no}: retryable ({resp})")
            if attempt_no == ARGS.max_attempts:
                break
            delay = min(ARGS.backoff_max, ARGS.backoff_base * (1.6 ** min(attempt_no - 1, 100)))
            delay = min(ARGS.backoff_max, delay * random.uniform(0.7, 1.3))
            retry_after = next((v for k, v in resp_headers.items() if k.lower() == "retry-after"), "")
            if retry_after.isdecimal():
                delay = max(delay, int(retry_after))
            wait_until = min(deadline, time.monotonic() + delay)
            while time.monotonic() < wait_until:
                if self._client_gone():
                    log(f"[{cport}] cancelled during backoff")
                    return
                time.sleep(min(0.1, max(0, wait_until - time.monotonic())))
        log(f"[{cport}] giving up after {attempt_no} attempts")
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

    def _stream_back(self, resp_headers, prelude, conn, resp):
        self.send_response(200)
        for key, value in resp_headers.items():
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        stream_socket = getattr(conn, "_relay_socket", None) or conn.sock
        if stream_socket is not None:
            stream_socket.settimeout(ARGS.timeout)
        try:
            self.wfile.write(prelude)
            self.wfile.flush()
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            log(f"[{self.client_address[1]}] stream interrupted; no relay replay")
        finally:
            conn.close()

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
    p.add_argument("--max-attempts", type=int, default=30)
    p.add_argument("--total-budget", type=float, default=300.0)
    p.add_argument("--backoff-base", type=float, default=1.0)
    p.add_argument("--backoff-max", type=float, default=8.0)
    p.add_argument("--commit-deadline", type=float, default=120.0,
                   help="max seconds to hold the stream head while deciding to retry")
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--max-body-bytes", type=int, default=64 << 20)
    p.add_argument("--max-concurrency", type=int, default=8)
    p.add_argument("--client-timeout", type=float, default=30.0)
    p.add_argument("--log", default="")
    ARGS = p.parse_args()

    if not 1 <= ARGS.port <= 65535 or not 1 <= ARGS.max_attempts <= 1000:
        p.error("invalid port or max-attempts (1..1000)")
    for name in ("timeout", "total_budget", "commit_deadline", "client_timeout", "backoff_base", "backoff_max"):
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
