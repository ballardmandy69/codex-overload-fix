import io
import json
import unittest
from email.message import Message
from unittest.mock import patch

from relay import relay


class FakeHandler:
    def __init__(self, body, headers):
        self.rfile = io.BytesIO(body)
        self.headers = Message()
        for key, value in headers.items():
            self.headers[key] = value


class RelayUnitTest(unittest.TestCase):
    def test_overload_is_retryable(self):
        payload = json.dumps({
            "type": "error",
            "error": {"code": "server_is_overloaded"},
        }).encode()
        verdict, info = relay.classify_event(payload)
        self.assertEqual(verdict, "retry")
        self.assertIn("server_is_overloaded", info)

    def test_overload_in_status_details_is_retryable(self):
        payload = json.dumps({
            "type": "response.failed",
            "response": {"status_details": {"reason": "server_overloaded"}},
        }).encode()
        verdict, info = relay.classify_event(payload)
        self.assertEqual(verdict, "retry")
        self.assertIn("server_overloaded", info)

    def test_top_level_overload_error_is_retryable(self):
        payload = json.dumps({
            "type": "response.failed",
            "code": "server_is_overloaded",
            "message": "Selected model is at capacity",
        }).encode()
        verdict, info = relay.classify_event(payload)
        self.assertEqual(verdict, "retry")
        self.assertIn("server_is_overloaded", info)

    def test_response_error_overload_is_retryable(self):
        payload = json.dumps({
            "type": "response.error",
            "response": {"error": {"message": "server overloaded"}},
        }).encode()
        verdict, _ = relay.classify_event(payload)
        self.assertEqual(verdict, "retry")

    def test_capacity_text_is_detected_before_streaming(self):
        self.assertTrue(relay.contains_overload_text(
            b'{"message":"Selected model is at capacity"}'
        ))
        self.assertFalse(relay.contains_overload_text(b'{"message":"normal response"}'))

    def test_cloudflare_html_403_is_retryable(self):
        body = b"<html><div class='blocked-icon'>Access denied</div></html>"
        headers = {"Content-Type": "text/html; charset=utf-8", "CF-Ray": "abc-HKG"}
        self.assertTrue(relay.is_cloudflare_block(403, headers, body))

    def test_generic_403_is_not_retryable(self):
        body = b'{"error":"invalid authentication"}'
        headers = {"Content-Type": "application/json"}
        self.assertFalse(relay.is_cloudflare_block(403, headers, body))

    def test_reasoning_and_tool_events_commit(self):
        for kind in (
            "response.reasoning_summary_text.delta",
            "response.function_call_arguments.delta",
        ):
            verdict, _ = relay.classify_event(json.dumps({"type": kind, "delta": "content"}).encode())
            self.assertEqual(verdict, "commit")

    def test_empty_output_metadata_does_not_commit(self):
        events = (
            {"type": "response.queued", "response": {}},
            {"type": "response.metadata", "response": {}},
            {"type": "response.output_item.added", "item": {"type": "message", "content": []}},
            {"type": "response.output_item.added", "item": {"type": "function_call", "name": "run"}},
            {"type": "response.output_item.done", "item": {"type": "reasoning", "summary": []}},
            {"type": "response.content_part.added", "part": {"type": "output_text", "text": ""}},
            {"type": "response.reasoning_summary_part.added", "part": {"type": "summary_text", "text": ""}},
            {"type": "response.output_text.delta", "delta": ""},
            {"type": "heartbeat"},
        )
        for evt in events:
            with self.subTest(event=evt):
                self.assertEqual(relay.classify_event(json.dumps(evt).encode()), (None, None))

    def test_populated_output_and_completed_tools_commit(self):
        events = (
            {"type": "response.output_item.added", "item": {
                "type": "message", "content": [{"type": "output_text", "text": "hello"}]}},
            {"type": "response.output_item.done", "item": {"type": "function_call", "arguments": ""}},
            {"type": "response.output_item.added", "item": {"type": "web_search_call"}},
            {"type": "response.content_part.added", "part": {"type": "output_text", "text": "hello"}},
        )
        for evt in events:
            with self.subTest(event=evt):
                self.assertEqual(relay.classify_event(json.dumps(evt).encode())[0], "commit")

    def test_sse_parser_handles_split_and_multiline_data(self):
        parser = relay.SSEParser()
        first = list(parser.feed(b"data: {\"type\":\"response.created\"}\r"))
        self.assertEqual(first, [])
        second = list(parser.feed(b"\ndata: {\"type\":\"error\",\n"))
        self.assertEqual(second, [])
        third = list(parser.feed(b"data: {\"code\":\"server_is_overloaded\"}}\n\n"))
        self.assertEqual(len(third), 1)
        self.assertIn(b"server_is_overloaded", third[0])

    def test_clash_http_port_is_loopback_only(self):
        self.assertEqual(relay.parse_proxy("127.0.0.1:7890"), ("http", "127.0.0.1", 7890))
        with self.assertRaises(ValueError):
            relay.parse_proxy("socks5://127.0.0.1:7891")
        with self.assertRaises(ValueError):
            relay.parse_proxy("http://example.invalid:7890")

    def test_request_size_and_chunk_framing_are_bounded(self):
        handler = FakeHandler(b"abc", {"Content-Length": "3"})
        self.assertEqual(relay.read_request_body(handler, 3), b"abc")
        with self.assertRaises(relay.RequestTooLarge):
            relay.read_request_body(FakeHandler(b"abcd", {"Content-Length": "4"}), 3)
        chunked = b"3\r\nabc\r\n0\r\n\r\n"
        handler = FakeHandler(chunked, {"Transfer-Encoding": "chunked"})
        self.assertEqual(relay.read_request_body(handler, 3), b"abc")

    def test_server_bind_does_not_perform_reverse_dns(self):
        with patch.object(relay.socket, "getfqdn", side_effect=AssertionError("unexpected DNS lookup")):
            with relay.LocalServer(("127.0.0.1", 0), relay.Handler) as server:
                self.assertEqual(server.server_name, "127.0.0.1")
                self.assertGreater(server.server_port, 0)

    def test_stream_observer_preserves_split_terminal_and_omits_content(self):
        observer = relay.StreamObserver(True)
        payload = b'data: {"type":"response.completed","response":{"text":"private reply"}}\n\n'
        observer.feed(payload[:20])
        self.assertEqual(observer.terminal, "none")
        observer.feed(payload[20:])
        self.assertEqual(observer.terminal, "response.completed")
        self.assertEqual(observer.inspection, "sse")

    def test_stream_observer_bounds_incomplete_events_and_leaves_opaque_bodies_alone(self):
        observer = relay.StreamObserver(True)
        observer.feed(b"data: " + b"x" * (relay.STREAM_INSPECTION_MAX_BYTES + 1))
        self.assertIsNone(observer.parser)
        self.assertEqual(observer.inspection, "stopped")
        self.assertTrue(observer.feed(b"more uninspected bytes"))
        opaque = relay.StreamObserver(False)
        opaque.feed(b'data: {"type":"response.completed"}\n\n')
        self.assertEqual(opaque.terminal, "none")
        self.assertEqual(opaque.inspection, "opaque")

    def test_compaction_is_classified_from_explicit_metadata_or_endpoint(self):
        metadata = json.dumps({"request_kind": "compaction", "compaction": {"trigger": "auto"}})
        with self.subTest(source="header with compressed body"):
            self.assertEqual(relay.classify_request(
                "/responses", {"X-Codex-Turn-Metadata": metadata, "Content-Encoding": "zstd"},
                b"compressed opaque bytes",
            ), ("compaction", "header"))
        with self.subTest(source="body compatibility metadata"):
            body = json.dumps({"client_metadata": {"x-codex-turn-metadata": metadata}}).encode()
            self.assertEqual(relay.classify_request("/responses", {}, body),
                             ("compaction", "client_metadata"))
        with self.subTest(source="native endpoint"):
            self.assertEqual(relay.classify_request("/backend-api/codex/responses/compact?mode=test", {}, b"{}"),
                             ("compaction", "endpoint"))

    def test_prompt_text_and_invalid_metadata_do_not_extend_request_timeout(self):
        body = json.dumps({"input": "request_kind compaction", "text": "please compact"}).encode()
        for metadata in ("not json", "[]", '{"request_kind":["compaction"]}',
                         '{"request_kind":"unknown"}', "x" * (relay.REQUEST_METADATA_MAX_BYTES + 1)):
            with self.subTest(metadata_size=len(metadata)):
                self.assertEqual(relay.classify_request("/responses", {"x-codex-turn-metadata": metadata}, body),
                                 ("unknown", "unavailable"))
        compressed = {"content-encoding": "zstd"}
        self.assertEqual(relay.classify_request("/responses", compressed, body), ("unknown", "unavailable"))

    def test_header_purpose_takes_precedence_and_body_scan_is_bounded(self):
        compact = json.dumps({"request_kind": "compaction"})
        body = json.dumps({"client_metadata": {"x-codex-turn-metadata": compact}}).encode()
        headers = {"x-codex-turn-metadata": json.dumps({"request_kind": "turn"})}
        self.assertEqual(relay.classify_request("/responses", headers, body), ("turn", "header"))
        with patch.object(relay, "REQUEST_METADATA_BODY_MAX_BYTES", len(body) - 1):
            self.assertEqual(relay.classify_request("/responses", {}, body), ("unknown", "unavailable"))


if __name__ == "__main__":
    unittest.main()
