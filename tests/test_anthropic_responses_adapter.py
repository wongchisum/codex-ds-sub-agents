from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import anthropic_adapter_protocol as protocol  # noqa: E402
import anthropic_responses_adapter as adapter  # noqa: E402


class AnthropicResponsesAdapterTests(unittest.TestCase):
    def test_audit_log_is_disabled_by_default(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            [
                "anthropic_responses_adapter",
                "--upstream-base-url",
                "https://upstream.invalid",
                "--service-id",
                "worker_1",
                "--model-catalog",
                "catalog.json",
            ],
        ):
            args = adapter.parse_args()
        self.assertIsNone(args.audit_log)

    def _request(
        self,
        upstream_response: dict,
        payload: dict,
        audit_path: Optional[Path] = None,
    ) -> tuple[int, dict, dict[str, str]]:
        audit_log = adapter.JsonAuditLog(audit_path) if audit_path is not None else None
        handler = object.__new__(adapter.AnthropicAdapterHandler)
        handler.path = "/responses"
        handler.headers = {"Authorization": "Bearer credential-secret"}
        handler.server = SimpleNamespace(
            max_output_tokens=4096,
            audit_log=audit_log,
        )
        handler._read_payload = mock.Mock(return_value=payload)
        handler._call_upstream = mock.Mock(return_value=upstream_response)
        sent = {}

        def send_json(status: int, body: dict, request_id: str) -> bool:
            sent.update(status=status, body=body, request_id=request_id)
            return True

        handler._json = mock.Mock(side_effect=send_json)
        handler._stream = mock.Mock(return_value=True)
        try:
            handler.do_POST()
            return sent["status"], sent["body"], {
                "X-Request-Id": sent["request_id"]
            }
        finally:
            if audit_log is not None:
                audit_log.close()

    def test_client_protocol_error_returns_400(self) -> None:
        status, body, _ = self._request(
            {"content": [{"type": "text", "text": "unused"}]},
            {"model": "model", "input": []},
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_request_error", body["error"]["type"])

    def test_missing_bearer_is_actionable_and_does_not_call_upstream(self) -> None:
        handler = object.__new__(adapter.AnthropicAdapterHandler)
        handler.path = "/responses"
        handler.headers = {}
        handler.server = SimpleNamespace(max_output_tokens=4096, audit_log=None)
        handler._json = mock.Mock()
        handler._call_upstream = mock.Mock()

        handler.do_POST()

        handler._json.assert_called_once()
        status, body, _request_id = handler._json.call_args.args
        self.assertEqual(401, status)
        self.assertEqual("authentication_error", body["error"]["type"])
        self.assertIn("provider auth command", body["error"]["message"])
        handler._call_upstream.assert_not_called()

    def test_invalid_upstream_structure_returns_502(self) -> None:
        status, body, _ = self._request(
            {"content": "not-an-array"},
            {"model": "model", "input": "hello", "stream": False},
        )
        self.assertEqual(502, status)
        self.assertEqual("upstream_invalid_response", body["error"]["type"])

    def test_invalid_upstream_json_uses_upstream_response_error(self) -> None:
        handler = object.__new__(adapter.AnthropicAdapterHandler)
        handler.server = mock.Mock(
            upstream_base_url="https://upstream.invalid",
            upstream_timeout=1.0,
            max_upstream_response_bytes=1024,
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"not-json"
        with mock.patch.object(adapter._UPSTREAM_OPENER, "open", return_value=response):
            with self.assertRaisesRegex(protocol.UpstreamResponseError, "invalid JSON"):
                handler._call_upstream("Bearer secret", {"model": "model"})
        response.close.assert_called_once_with()

    def test_audit_contains_only_structural_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "audit.jsonl"
            status, _, headers = self._request(
                {
                    "model": "model",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 3,
                        "cache_read_input_tokens": 2,
                    },
                },
                {
                    "model": "model",
                    "instructions": "TOP SECRET PROMPT",
                    "input": "private prompt text",
                    "max_output_tokens": 64000,
                    "stream": False,
                },
                audit_path,
            )
            record_text = audit_path.read_text(encoding="utf-8")
            record = json.loads(record_text)
        self.assertEqual(200, status)
        self.assertEqual(headers["X-Request-Id"], record["request_id"])
        self.assertEqual("model", record["model"])
        self.assertEqual(64000, record["requested_max_output_tokens"])
        self.assertEqual(4096, record["applied_max_output_tokens"])
        self.assertEqual(200, record["status"])
        self.assertIsNone(record["error_category"])
        self.assertIsInstance(record["duration_ms"], int)
        self.assertEqual(
            {"input_tokens": 12, "output_tokens": 3, "cache_read_input_tokens": 2},
            record["upstream_usage"],
        )
        self.assertNotIn("TOP SECRET PROMPT", record_text)
        self.assertNotIn("private prompt text", record_text)
        self.assertNotIn("credential-secret", record_text)

    def test_upstream_body_limit_is_enforced(self) -> None:
        with self.assertRaisesRegex(protocol.UpstreamResponseError, "5-byte limit"):
            adapter._read_upstream_body(io.BytesIO(b"123456"), 5)

    def test_json_response_ignores_broken_pipe(self) -> None:
        handler = object.__new__(adapter.AnthropicAdapterHandler)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = mock.Mock()
        handler.wfile.write.side_effect = BrokenPipeError()
        self.assertFalse(handler._json(200, {"status": "ok"}))

    def test_stream_response_ignores_connection_reset(self) -> None:
        handler = object.__new__(adapter.AnthropicAdapterHandler)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock(side_effect=ConnectionResetError())
        handler.wfile = mock.Mock()
        self.assertFalse(handler._stream({}, "req_test"))

    def test_health_exposes_service_bound_fingerprint_without_url_secrets(self) -> None:
        handler = object.__new__(adapter.AnthropicAdapterHandler)
        handler.path = "/health"
        normalized = adapter.normalize_upstream_base_url(
            "https://Gateway.Example:8443/anthropic/"
        )
        handler.server = SimpleNamespace(
            service_id="deepseek_anthropic",
            service_fingerprint=adapter.service_fingerprint(
                "deepseek_anthropic",
                normalized,
            ),
            upstream_host="gateway.example",
        )
        sent = {}
        handler._json = mock.Mock(
            side_effect=lambda status, body: sent.update(status=status, body=body)
        )
        handler.do_GET()
        self.assertEqual(200, sent["status"])
        self.assertEqual("deepseek_anthropic", sent["body"]["service_id"])
        self.assertEqual("anthropic", sent["body"]["identity"]["provider"])
        self.assertEqual("gateway.example", sent["body"]["identity"]["upstream_host"])
        self.assertTrue(sent["body"]["fingerprint"].startswith("sha256:"))
        self.assertNotIn("anthropic/", json.dumps(sent["body"]))

    def test_service_fingerprint_normalizes_url_and_binds_path(self) -> None:
        first = adapter.service_fingerprint(
            "worker",
            "https://EXAMPLE.com:443/api/",
        )
        equivalent = adapter.service_fingerprint(
            "worker",
            "https://example.com/api",
        )
        different_path = adapter.service_fingerprint(
            "worker",
            "https://example.com/other",
        )
        self.assertEqual(first, equivalent)
        self.assertNotEqual(first, different_path)
        with self.assertRaisesRegex(ValueError, "credentials"):
            adapter.service_fingerprint(
                "worker",
                "https://secret@example.com/api",
            )

    def test_redirects_are_not_followed_with_authorization(self) -> None:
        original = adapter.urllib.request.Request(
            "https://upstream.example/v1/messages",
            headers={"Authorization": "Bearer secret"},
        )
        redirected = adapter._NoRedirectHandler().redirect_request(
            original,
            io.BytesIO(),
            302,
            "Found",
            {},
            "https://attacker.example/steal",
        )
        self.assertIsNone(redirected)

        handler = object.__new__(adapter.AnthropicAdapterHandler)
        handler.server = SimpleNamespace(
            upstream_base_url="https://upstream.example",
            upstream_timeout=1.0,
            max_upstream_response_bytes=1024,
        )
        error = adapter.urllib.error.HTTPError(
            "https://upstream.example/v1/messages",
            302,
            "Found",
            {"Content-Type": "text/plain", "Location": "https://attacker.example"},
            io.BytesIO(b"redirect denied"),
        )
        with mock.patch.object(
            adapter._UPSTREAM_OPENER,
            "open",
            side_effect=error,
        ) as open_mock:
            with self.assertRaises(adapter.UpstreamError) as caught:
                handler._call_upstream(
                    "Bearer credential-secret",
                    {"model": "model", "stream": False},
                )
        self.assertEqual(302, caught.exception.status)
        self.assertEqual(1, open_mock.call_count)
        request = open_mock.call_args.args[0]
        self.assertEqual("Bearer credential-secret", request.get_header("Authorization"))

    def test_stream_error_after_http_200_emits_error_and_failed_events(self) -> None:
        for upstream_code in ("overloaded_error", "rate_limit_error"):
            with self.subTest(upstream_code=upstream_code):
                handler = self._streaming_handler()
                upstream = io.BytesIO(self._sse_bytes(
                    (
                        "message_start",
                        {
                            "type": "message_start",
                            "message": {
                                "model": "model",
                                "content": [],
                                "usage": {"input_tokens": 3, "output_tokens": 1},
                            },
                        },
                    ),
                    (
                        "error",
                        {
                            "type": "error",
                            "error": {"type": upstream_code, "message": "busy"},
                        },
                    ),
                ))
                summary, category = handler._stream_anthropic_response(
                    upstream,
                    {"model": "model"},
                    {},
                    "req_test",
                )
                wire_lines = handler.wfile.getvalue().decode("utf-8").splitlines()
                response_events = [
                    json.loads(line.removeprefix("data: "))
                    for line in wire_lines
                    if line.startswith("data: ")
                ]
                self.assertEqual(
                    [
                        "response.created",
                        "response.in_progress",
                        "error",
                        "response.failed",
                    ],
                    [event["type"] for event in response_events],
                )
                error_event = response_events[-2]
                failed_event = response_events[-1]
                self.assertEqual(upstream_code, error_event["code"])
                self.assertEqual(
                    upstream_code,
                    failed_event["response"]["error"]["code"],
                )
                self.assertEqual("upstream_stream_error", category)
                self.assertEqual(3, summary["usage"]["input_tokens"])

    def test_whitespace_only_stream_fails_after_http_200(self) -> None:
        handler = self._streaming_handler()
        upstream = io.BytesIO(self._sse_bytes(
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "model": "model",
                        "content": [],
                        "usage": {"input_tokens": 3, "output_tokens": 1},
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": " \n\t"},
                },
            ),
            (
                "content_block_stop",
                {"type": "content_block_stop", "index": 0},
            ),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 2},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ))
        _, category = handler._stream_anthropic_response(
            upstream,
            {"model": "model"},
            {},
            "req_test",
        )
        event_types = [
            line.removeprefix("event: ")
            for line in handler.wfile.getvalue().decode("utf-8").splitlines()
            if line.startswith("event: ")
        ]
        self.assertNotIn("response.completed", event_types)
        self.assertEqual(["error", "response.failed"], event_types[-2:])
        self.assertEqual("upstream_invalid_stream", category)

    def test_disconnect_stops_upstream_reads_and_closes_response(self) -> None:
        class TrackingStream(io.BytesIO):
            def __init__(self, value: bytes) -> None:
                super().__init__(value)
                self.readline_calls = 0
                self.was_closed = False

            def readline(self, size: int = -1) -> bytes:
                self.readline_calls += 1
                return super().readline(size)

            def close(self) -> None:
                self.was_closed = True

        upstream = TrackingStream(self._sse_bytes(
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {"model": "model", "content": [], "usage": {}},
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
        ))
        handler = self._streaming_handler()
        handler.server.upstream_base_url = "https://upstream.example"
        handler.server.upstream_timeout = 1.0
        handler.wfile = mock.Mock()
        handler.wfile.write.side_effect = BrokenPipeError()
        with mock.patch.object(
            adapter._UPSTREAM_OPENER,
            "open",
            return_value=upstream,
        ) as open_mock:
            _, category = handler._call_upstream_stream(
                "Bearer secret",
                {"model": "model", "stream": True},
                {"model": "model"},
                {},
                "req_test",
            )
        self.assertEqual("client_disconnected", category)
        self.assertTrue(upstream.was_closed)
        self.assertEqual(3, upstream.readline_calls)
        sent_request = open_mock.call_args.args[0]
        self.assertTrue(json.loads(sent_request.data)["stream"])

    def test_stream_byte_limit_counts_all_sse_bytes(self) -> None:
        stream = io.BytesIO(b"event: ping\ndata: {\"type\":\"ping\"}\n\n")
        with self.assertRaisesRegex(adapter.UpstreamStreamLimitError, "10-byte limit"):
            list(adapter._iter_anthropic_sse(stream, 10))

    def test_stream_error_category_is_written_to_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "audit.jsonl"
            audit_log = adapter.JsonAuditLog(audit_path)
            handler = object.__new__(adapter.AnthropicAdapterHandler)
            handler.path = "/responses"
            handler.headers = {"Authorization": "Bearer secret"}
            handler.server = SimpleNamespace(
                max_output_tokens=4096,
                audit_log=audit_log,
            )
            handler._read_payload = mock.Mock(return_value={
                "model": "model",
                "input": "hello",
                "stream": True,
            })
            handler._call_upstream_stream = mock.Mock(return_value=(
                {"model": "model", "usage": {"input_tokens": 2}},
                "upstream_invalid_stream",
            ))
            try:
                handler.do_POST()
            finally:
                audit_log.close()
            record = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(200, record["status"])
        self.assertEqual("upstream_invalid_stream", record["error_category"])

    @staticmethod
    def _sse_bytes(*events: tuple[str, dict]) -> bytes:
        return "".join(
            "event: " + event_type + "\n"
            + "data: " + json.dumps(event, separators=(",", ":")) + "\n\n"
            for event_type, event in events
        ).encode("utf-8")

    @staticmethod
    def _streaming_handler() -> adapter.AnthropicAdapterHandler:
        handler = object.__new__(adapter.AnthropicAdapterHandler)
        handler.server = SimpleNamespace(max_upstream_response_bytes=1024 * 1024)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = io.BytesIO()
        return handler


if __name__ == "__main__":
    unittest.main()
