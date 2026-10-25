import io
import json
import unittest
from email.message import Message

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
            "response.output_item.added",
            "response.function_call_arguments.delta",
        ):
            verdict, _ = relay.classify_event(json.dumps({"type": kind}).encode())
            self.assertEqual(verdict, "commit")

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


if __name__ == "__main__":
    unittest.main()
