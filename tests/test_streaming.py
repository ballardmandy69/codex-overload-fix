"""Socket-level regressions for precommit classification and cancellation."""

import http.client
import json
import re
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from relay import relay


def event(kind, **fields):
    return b"data: " + json.dumps(dict(type=kind, **fields)).encode() + b"\n\n"


@contextmanager
def upstream(chunks, hold=False, content_type=None, retry_after=None, chunked=False):
    """Serve controlled chunks and optionally keep the response open."""
    release = threading.Event()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        """Controlled test upstream. @author lyt @date 2026-11-14"""

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append(self.path)
            self.send_response(200)
            if content_type:
                self.send_header("Content-Type", content_type)
            if retry_after:
                self.send_header("Retry-After", retry_after)
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                response_chunks = chunks(len(calls)) if callable(chunks) else chunks
                for chunk in response_chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                if hold:
                    release.wait(3)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()
    args = SimpleNamespace(
        log=None, upstream_scheme="http", upstream_host="127.0.0.1",
        upstream_port=server.server_port, upstream_prefix="", proxy=None,
        timeout=2, client_timeout=2, max_body_bytes=1048576, total_budget=2,
        commit_deadline=1, max_attempts=3, backoff_base=0, backoff_max=0,
        stream_idle_timeout=0.5,
        compaction_timeout=1,
    )
    try:
        with patch.object(relay, "ARGS", args):
            yield calls
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


class StreamingTest(unittest.TestCase):
    """Verify real HTTP behavior. @author lyt @date 2026-11-14"""

    def setUp(self):
        log_patch = patch.object(relay, "log")
        self.logs = log_patch.start()
        self.addCleanup(log_patch.stop)

    def attempt(self, cancelled=lambda: False, deadline=1):
        result = relay.attempt("/responses", {}, b"{}", 2,
                               time.monotonic() + deadline, cancelled)
        if result[4] is not None:
            self.addCleanup(relay.close_upstream, result[4])
        return result

    def test_missing_type_commits_before_eof(self):
        payload = event("response.output_text.delta", delta="hello")
        with upstream([payload], hold=True):
            result = self.attempt()
            self.assertEqual(result[0], "stream")
            self.assertEqual(result[2]["Content-Type"], "text/event-stream")
            self.assertEqual(result[3], payload)

    def test_split_bom_and_fields(self):
        payload = b"\xef\xbb\xbf" + event("response.output_text.delta", delta="hello")
        with upstream([payload[:1], payload[1:4], payload[4:]], hold=True):
            result = self.attempt()
            self.assertEqual(result[0], "stream")
            self.assertEqual(result[3], payload)

    def test_overload_after_lifecycle_retries(self):
        payload = (event("response.created", response={})
                   + event("response.output_item.added", item={"type": "reasoning", "summary": []})
                   + event("response.content_part.added", part={"type": "output_text", "text": ""})) + event(
            "error", error={"code": "server_is_overloaded"})
        with upstream([payload], hold=True):
            result = self.attempt()
            self.assertEqual(result[0], "retry")
            self.assertIn("overload", result[5])

    def test_normal_output_mentioning_overload_commits(self):
        payload = event("response.output_item.added", item={
            "text": "Selected model is at capacity"}) + event(
                "error", error={"code": "server_is_overloaded"})
        with upstream([payload], hold=True):
            self.assertEqual(self.attempt()[0], "stream")

    def test_nonretryable_error_commits(self):
        with upstream([event("error", error={"code": "invalid_api_key"})]):
            self.assertEqual(self.attempt()[0], "stream")

    def test_json_response_is_preserved(self):
        payload = b'{"result":"ok"}'
        with upstream([payload], content_type="application/json"):
            result = self.attempt()
            self.assertEqual(result[0], "stream")
            self.assertEqual(result[3], payload)

    def test_timing_distinguishes_tool_output_from_text_and_omits_content(self):
        tool = event("response.function_call_arguments.delta", delta="private-tool-arguments")
        text = event("response.output_text.delta", delta="private-body-text")
        terminal = event("response.completed", response={"status": "completed"})

        def chunks(_):
            yield tool
            time.sleep(0.12)
            yield text + terminal

        self.relay_response(chunks, tool + text + terminal, 1)
        logs = "\n".join(call.args[0] for call in self.logs.call_args_list)
        self.assertEqual(logs.count("first text observed:"), 1)
        timing = next(line for line in logs.splitlines() if "stream timing:" in line)
        first_text = float(re.search(r"first_text=([\d.]+)s", timing).group(1))
        gap = float(re.search(r"max_progress_gap=([\d.]+)s", timing).group(1))
        self.assertGreaterEqual(first_text, 0.1)
        self.assertGreaterEqual(gap, 0.1)
        self.assertIn("downstream_write=", timing)
        self.assertIn("max_write=", timing)
        self.assertIn("headers_wait=", logs)
        self.assertIn("upload=", logs)
        self.assertNotIn("private-body-text", logs)
        self.assertNotIn("private-tool-arguments", logs)

    def test_timing_preserves_failed_attempt_phase_and_non_text_response(self):
        with upstream([event("response.created", response={})], hold=True):
            result = self.attempt(deadline=0.15)
            self.assertEqual(result[0], "retry")
        logs = "\n".join(call.args[0] for call in self.logs.call_args_list)
        self.assertIn("attempt timing: phase=first_output", logs)
        self.assertIn("retained=False", logs)
        self.relay_response([b'{"ok":true}'], b'{"ok":true}', 1, content_type="application/json")
        logs = "\n".join(call.args[0] for call in self.logs.call_args_list)
        self.assertIn("first_text=unavailable", logs)
        self.assertNotIn("first text observed:", logs)

    def test_deadline_interrupts_blocked_read(self):
        with upstream([event("response.created", response={})], hold=True):
            started = time.monotonic()
            result = self.attempt(deadline=0.15)
            self.assertEqual(result[5], "transport_502")
            self.assertLess(time.monotonic() - started, 1)

    def test_cancellation_interrupts_blocked_read(self):
        started = time.monotonic()
        with upstream([event("response.created", response={})], hold=True):
            result = self.attempt(cancelled=lambda: time.monotonic() - started > 0.15)
            self.assertEqual(result[5], "transport_502")
            self.assertLess(time.monotonic() - started, 1)

    def relay_response(self, chunks, expected, expected_calls, status=200, retry_after=None, settings=None,
                       headers=None, body=b"{}", path="/responses", content_type="text/event-stream",
                       chunked=False):
        with upstream(chunks, content_type=content_type, retry_after=retry_after, chunked=chunked) as calls:
            for name, value in (settings or {}).items():
                setattr(relay.ARGS, name, value)
            with patch.object(relay, "REQUEST_SEM", threading.BoundedSemaphore(4)):
                server = relay.LocalServer(("127.0.0.1", 0), relay.Handler)
                thread = threading.Thread(target=server.serve_forever,
                                          kwargs={"poll_interval": 0.02})
                thread.start()
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                try:
                    client.request("POST", path, body=body, headers=headers or {})
                    response = client.getresponse()
                    self.assertEqual(response.status, status)
                    data = response.read()
                    if expected is not None:
                        self.assertEqual(data, expected)
                    self.assertEqual(len(calls), expected_calls)
                    return data
                finally:
                    client.close()
                    server.shutdown()
                    server.server_close()
                    thread.join()

    def test_relay_does_not_replay_after_output(self):
        payload = event("response.function_call_arguments.delta", delta="{}") + event(
            "error", error={"code": "server_is_overloaded"})
        self.relay_response([payload], payload, 1)

    def test_relay_retries_overload_then_streams_success(self):
        overload = event("error", error={"code": "server_is_overloaded"})
        output = event("response.output_text.delta", delta="hello")
        self.relay_response(lambda number: [overload if number == 1 else output], output, 2)

    def test_relay_does_not_replay_truncated_committed_stream(self):
        output = event("response.output_text.delta", delta="partial")
        self.relay_response([output], output, 1)
        summaries = [call.args[0] for call in self.logs.call_args_list if "stream ended:" in call.args[0]]
        self.assertEqual(len(summaries), 1)
        self.assertIn("reason=upstream_eof; terminal=none", summaries[0])

    def test_completed_stream_summary_records_outcome_bytes_and_elapsed_without_content(self):
        payload = (event("response.output_text.delta", delta="private response text")
                   + event("response.completed", response={"status": "completed"}))
        self.relay_response([payload], payload, 1)
        summaries = [call.args[0] for call in self.logs.call_args_list if "stream ended:" in call.args[0]]
        self.assertEqual(len(summaries), 1)
        self.assertIn("reason=completed; terminal=response.completed", summaries[0])
        self.assertIn(f"forwarded={len(payload)}B", summaries[0])
        self.assertIn("elapsed=", summaries[0])
        self.assertNotIn("private response text", summaries[0])

    def test_failed_terminal_after_output_is_diagnosed_without_replay(self):
        payload = (event("response.output_text.delta", delta="partial")
                   + event("response.failed", response={"error": {"code": "server_is_overloaded"}}))
        self.relay_response([payload], payload, 1)
        self.assertTrue(any("terminal=response.failed" in call.args[0]
                            for call in self.logs.call_args_list))

    def test_truncated_chunk_records_exception_type_and_io_stage_without_replay(self):
        output = event("response.output_text.delta", delta="partial")
        framed_output = f"{len(output):x}\r\n".encode() + output + b"\r\n"
        self.relay_response([framed_output, b"10\r\nshort"], output + b"short", 1, chunked=True)
        summaries = [call.args[0] for call in self.logs.call_args_list if "stream ended:" in call.args[0]]
        self.assertEqual(len(summaries), 1)
        self.assertIn("reason=stream_io_error; terminal=none", summaries[0])
        self.assertIn("error_type=IncompleteRead", summaries[0])
        self.assertIn("io_stage=upstream_read", summaries[0])

    def test_relay_bounds_empty_stream_failures(self):
        self.relay_response([], b"relay: upstream stream ended before output\n", 3, status=502)

    def test_transport_failures_back_off_before_repeating(self):
        with patch.object(relay.random, "uniform", return_value=1):
            started = time.monotonic()
            self.relay_response([], b"relay: upstream stream ended before output\n", 3, status=502,
                                settings={"backoff_base": 0.05, "backoff_max": 0.1})
        self.assertGreaterEqual(time.monotonic() - started, 0.15)
        logs = "\n".join(call.args[0] for call in self.logs.call_args_list)
        self.assertIn("delay=0.050s; reason=transport_502", logs)
        self.assertIn("delay=0.100s; reason=transport_502", logs)
        self.assertNotIn("immediate retry", logs)

    def test_retry_wait_exceeding_remaining_budget_does_not_resubmit(self):
        overload = event("error", error={"code": "server_is_overloaded"})
        with patch.object(relay.random, "uniform", return_value=1):
            self.relay_response([overload], overload, 1,
                                settings={"total_budget": 0.2, "backoff_base": 0.5, "backoff_max": 4})
        self.assertTrue(any("retry wait exceeds cap or remaining budget" in call.args[0]
                            for call in self.logs.call_args_list))

    def test_long_retry_after_returns_without_waiting(self):
        overload = event("error", error={"code": "server_is_overloaded"})
        started = time.monotonic()
        self.relay_response([overload], overload, 1, retry_after="120")
        self.assertLess(time.monotonic() - started, 1)

    def test_placeholders_and_heartbeats_do_not_escape_first_output_budget(self):
        def chunks(_):
            yield event("response.output_item.added", item={"type": "message", "content": []})
            for _ in range(12):
                time.sleep(0.05)
                yield b": keepalive\n\n"

        started = time.monotonic()
        self.relay_response(
            chunks, b"relay: timed out waiting for upstream output\n", 2, status=502,
            settings={"commit_deadline": 0.2, "total_budget": 0.35},
        )
        self.assertLess(time.monotonic() - started, 1.2)
        messages = [call.args[0] for call in self.logs.call_args_list]
        self.assertFalse(any("ok, streaming" in message for message in messages))
        self.assertTrue(any("reason=precommit_deadline" in message for message in messages))
        self.assertTrue(any("giving up after 2 attempts" in message for message in messages))

    def test_heartbeats_do_not_extend_stream_progress_timeout(self):
        output = event("response.output_text.delta", delta="hello")
        terminal = event("response.completed", response={"status": "completed"})

        def chunks(_):
            yield output
            for _ in range(12):
                time.sleep(0.05)
                yield b": keepalive\n\n"
            yield terminal

        data = self.relay_response(chunks, None, 1, settings={"stream_idle_timeout": 0.2})
        self.assertTrue(data.startswith(output))
        self.assertNotIn(terminal, data)
        self.assertTrue(any("reason=stream_idle_timeout" in call.args[0]
                            for call in self.logs.call_args_list))

    def test_actual_progress_resets_stream_timeout(self):
        outputs = [event("response.output_text.delta", delta=str(index)) for index in range(5)]
        terminal = event("response.completed", response={"status": "completed"})

        def chunks(_):
            for index, output in enumerate(outputs):
                if index:
                    time.sleep(0.12)
                yield output
            yield terminal

        self.relay_response(chunks, b"".join(outputs) + terminal, 1,
                            settings={"stream_idle_timeout": 0.3})
        self.assertFalse(any("reason=stream_idle_timeout" in call.args[0]
                             for call in self.logs.call_args_list))

    def test_compaction_survives_normal_first_output_and_stream_idle_limits(self):
        output = event("response.output_text.delta", delta="summary")
        terminal = event("response.completed", response={"status": "completed"})

        def chunks(_):
            yield event("response.created", response={})
            time.sleep(0.3)
            yield output
            time.sleep(0.3)
            yield terminal

        payload = event("response.created", response={}) + output + terminal
        headers = {"x-codex-turn-metadata": json.dumps({"request_kind": "compaction"})}
        self.relay_response(
            chunks, payload, 1, headers=headers,
            settings={"total_budget": 0.15, "commit_deadline": 0.15,
                      "stream_idle_timeout": 0.15, "timeout": 0.15, "compaction_timeout": 1},
        )
        self.assertTrue(any("kind=compaction; source=header; budget=1s" in call.args[0]
                            for call in self.logs.call_args_list))
        self.assertTrue(any("reason=completed; terminal=response.completed" in call.args[0]
                            for call in self.logs.call_args_list))

    def test_ordinary_request_keeps_normal_first_output_deadline(self):
        def chunks(_):
            yield event("response.created", response={})
            time.sleep(0.4)
            yield event("response.output_text.delta", delta="late")

        headers = {"x-codex-turn-metadata": json.dumps({"request_kind": "turn"})}
        self.relay_response(
            chunks, b"relay: timed out waiting for upstream output\n", 1, status=502, headers=headers,
            settings={"total_budget": 0.15, "commit_deadline": 0.15, "compaction_timeout": 1,
                      "max_attempts": 1},
        )
        self.assertTrue(any("kind=turn; source=header" in call.args[0] for call in self.logs.call_args_list))

    def test_native_compact_endpoint_uses_compaction_budget_for_json(self):
        payload = b'{"output":[{"type":"compaction","encrypted_content":"opaque"}]}'

        def chunks(_):
            time.sleep(0.3)
            yield payload

        self.relay_response(chunks, payload, 1, path="/responses/compact", content_type="application/json",
                            settings={"total_budget": 0.1, "commit_deadline": 0.1, "compaction_timeout": 1})
        self.assertTrue(any("kind=compaction; source=endpoint" in call.args[0]
                            for call in self.logs.call_args_list))

    def test_body_marked_compaction_still_has_a_bounded_deadline(self):
        def chunks(_):
            yield event("response.created", response={})
            for _ in range(12):
                time.sleep(0.05)
                yield b": keepalive\n\n"

        body = json.dumps({"client_metadata": {
            "x-codex-turn-metadata": json.dumps({"request_kind": "compaction"})}}).encode()
        started = time.monotonic()
        self.relay_response(chunks, b"relay: timed out waiting for upstream output\n", 1, status=502, body=body,
                            settings={"compaction_timeout": 0.2})
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(any("kind=compaction; source=client_metadata; budget=0.2s" in call.args[0]
                            for call in self.logs.call_args_list))


if __name__ == "__main__":
    unittest.main()
