#!/usr/bin/env python3
"""Expose an OpenAI Responses-compatible loopback endpoint for Anthropic Messages."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator, Mapping, Optional, Tuple

from anthropic_adapter_protocol import (
    AnthropicStreamTranslator,
    ProtocolError,
    UpstreamResponseError,
    UpstreamStreamError,
    build_anthropic_request,
    build_responses_response,
    response_events,
)


MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_UPSTREAM_RESPONSE_BYTES = 8 * 1024 * 1024
UPSTREAM_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
SERVICE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class JsonAuditLog:
    """Write one credential- and prompt-free JSON record per adapter request."""

    def __init__(self, target: Path) -> None:
        self.target = target
        self._lock = threading.Lock()
        descriptor = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            self._stream = os.fdopen(descriptor, "a", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise

    def emit(self, record: Mapping[str, object]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            self._stream.write(line)
            self._stream.flush()

    def close(self) -> None:
        with self._lock:
            if not self._stream.closed:
                self._stream.close()


class UpstreamError(RuntimeError):
    def __init__(self, status: int, body: bytes, content_type: str) -> None:
        super().__init__(f"upstream returned HTTP {status}")
        self.status = status
        self.body = body
        self.content_type = content_type


class UpstreamStreamLimitError(UpstreamResponseError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        # Returning None makes urllib surface every 30x as HTTPError. This is
        # essential because Request's Authorization header must never follow a
        # provider-controlled Location URL.
        return None


_UPSTREAM_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def normalize_upstream_base_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError as error:
        raise ValueError(f"invalid upstream base URL: {error}") from error
    if parsed.scheme.lower() != "https" or not hostname:
        raise ValueError("upstream base URL must use HTTPS and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("upstream base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("upstream base URL must not contain a query or fragment")
    try:
        normalized_host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ValueError("upstream base URL contains an invalid host") from error
    authority_host = (
        f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    )
    authority = authority_host
    if port is not None and port != 443:
        authority += f":{port}"
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit(("https", authority, path, "", ""))


def service_fingerprint(service_id: str, upstream_base_url: str) -> str:
    if SERVICE_ID_PATTERN.fullmatch(service_id) is None:
        raise ValueError(
            "service ID must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}"
        )
    normalized_url = normalize_upstream_base_url(upstream_base_url)
    material = (
        b"anthropic_messages\0"
        + service_id.encode("utf-8")
        + b"\0"
        + normalized_url.encode("utf-8")
    )
    return "sha256:" + hashlib.sha256(material).hexdigest()


class AnthropicAdapterServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        upstream_base_url: str,
        timeout: float,
        model_catalog: Mapping[str, object],
        max_output_tokens: int,
        service_id: str,
        max_upstream_response_bytes: int = MAX_UPSTREAM_RESPONSE_BYTES,
        audit_log: Optional[JsonAuditLog] = None,
    ) -> None:
        normalized_upstream_url = normalize_upstream_base_url(upstream_base_url)
        fingerprint = service_fingerprint(service_id, normalized_upstream_url)
        self.audit_log = audit_log
        super().__init__(address, AnthropicAdapterHandler)
        self.upstream_base_url = normalized_upstream_url
        self.upstream_host = urllib.parse.urlsplit(self.upstream_base_url).hostname
        self.upstream_timeout = timeout
        self.model_catalog = model_catalog
        self.max_output_tokens = max_output_tokens
        self.max_upstream_response_bytes = max_upstream_response_bytes
        self.service_id = service_id
        self.service_fingerprint = fingerprint

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            if self.audit_log is not None:
                self.audit_log.close()


class AnthropicAdapterHandler(BaseHTTPRequestHandler):
    server: AnthropicAdapterServer

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write("anthropic-adapter: " + format % args + "\n")

    def _raw(
        self,
        status: int,
        body: bytes,
        content_type: str,
        request_id: Optional[str] = None,
    ) -> bool:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if request_id is not None:
                self.send_header("X-Request-Id", request_id)
            self.end_headers()
            self.wfile.write(body)
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _json(
        self,
        status: int,
        payload: Mapping[str, object],
        request_id: Optional[str] = None,
    ) -> bool:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._raw(status, body, "application/json", request_id)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._json(200, {
                "status": "ok",
                "adapter": "anthropic_messages",
                "service_id": self.server.service_id,
                "fingerprint": self.server.service_fingerprint,
                "identity": {
                    "provider": "anthropic",
                    "name": "anthropic_messages",
                    "upstream_host": self.server.upstream_host,
                },
            })
            return
        if path in ("/models", "/v1/models"):
            self._json(200, self.server.model_catalog)
            return
        self._json(404, {"error": {"message": "not found", "type": "not_found"}})

    def do_POST(self) -> None:
        started = time.monotonic()
        request_id = "req_" + uuid.uuid4().hex
        payload: Optional[dict] = None
        upstream_request: Optional[dict] = None
        anthropic: Optional[dict] = None
        status = 500
        error_category: Optional[str] = "internal_error"
        path = self.path.split("?", 1)[0]
        try:
            if path not in ("/responses", "/v1/responses"):
                status = 404
                error_category = "not_found"
                self._json(
                    status,
                    {"error": {"message": "not found", "type": error_category}},
                    request_id,
                )
                return
            authorization = self.headers.get("Authorization")
            if not authorization or not authorization.startswith("Bearer "):
                status = 401
                error_category = "authentication_error"
                self._json(
                    status,
                    {
                        "error": {
                            "message": "missing bearer credential",
                            "type": error_category,
                        }
                    },
                    request_id,
                )
                return
            payload = self._read_payload()
            upstream_request, tool_types = build_anthropic_request(
                payload,
                self.server.max_output_tokens,
            )
            if payload.get("stream") is False:
                anthropic = self._call_upstream(authorization, upstream_request)
                response = build_responses_response(anthropic, payload, tool_types)
                status = 200
                error_category = None
                self._json(status, response, request_id)
            else:
                anthropic, error_category = self._call_upstream_stream(
                    authorization,
                    upstream_request,
                    payload,
                    tool_types,
                    request_id,
                )
                status = 200
        except UpstreamStreamLimitError as error:
            status = 502
            error_category = "upstream_stream_too_large"
            self._json(
                status,
                {"error": {"message": str(error), "type": error_category}},
                request_id,
            )
        except UpstreamStreamError as error:
            status = 502
            error_category = "upstream_stream_error"
            self._json(
                status,
                {"error": {"message": error.message, "type": error_category}},
                request_id,
            )
        except UpstreamResponseError as error:
            status = 502
            error_category = "upstream_invalid_response"
            self._json(
                status,
                {"error": {"message": str(error), "type": error_category}},
                request_id,
            )
        except ProtocolError as error:
            status = 400
            error_category = "invalid_request_error"
            self._json(
                status,
                {"error": {"message": str(error), "type": error_category}},
                request_id,
            )
        except UpstreamError as error:
            status = error.status
            error_category = "upstream_http_error"
            self._raw(
                status,
                error.body,
                error.content_type or "application/json",
                request_id,
            )
        except (
            TimeoutError,
            urllib.error.URLError,
            ConnectionError,
            http.client.HTTPException,
            OSError,
        ) as error:
            status = 503
            error_category = "upstream_unavailable"
            self._json(
                status,
                {"error": {"message": str(error), "type": error_category}},
                request_id,
            )
        finally:
            self._audit_request(
                request_id=request_id,
                payload=payload,
                upstream_request=upstream_request,
                anthropic=anthropic,
                status=status,
                error_category=error_category,
                started=started,
            )

    def _read_payload(self) -> dict:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError as error:
            raise ProtocolError("invalid Content-Length") from error
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ProtocolError("request body size is invalid")
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProtocolError(f"invalid JSON request: {error}") from error
        if not isinstance(payload, dict):
            raise ProtocolError("request body must be an object")
        return payload

    def _call_upstream(self, authorization: str, payload: Mapping[str, object]) -> dict:
        upstream = self._open_upstream(authorization, payload)
        try:
            body = _read_upstream_body(
                upstream,
                self.server.max_upstream_response_bytes,
            )
        finally:
            upstream.close()  # type: ignore[attr-defined]
        try:
            parsed = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpstreamResponseError(f"upstream returned invalid JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise UpstreamResponseError("upstream response must be an object")
        return parsed

    def _open_upstream(
        self,
        authorization: str,
        payload: Mapping[str, object],
    ) -> object:
        request = urllib.request.Request(
            self.server.upstream_base_url + "/v1/messages",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": authorization,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
                "User-Agent": "codex-anthropic-responses-adapter/1",
            },
        )
        try:
            return _UPSTREAM_OPENER.open(
                request,
                timeout=self.server.upstream_timeout,
            )
        except urllib.error.HTTPError as error:
            content_type = (
                error.headers.get("Content-Type", "application/json")
                if error.headers is not None
                else "application/json"
            )
            try:
                body = _read_upstream_body(
                    error,
                    self.server.max_upstream_response_bytes,
                )
            finally:
                error.close()
            raise UpstreamError(
                error.code,
                body,
                content_type,
            ) from error

    def _call_upstream_stream(
        self,
        authorization: str,
        upstream_request: Mapping[str, object],
        request_payload: Mapping[str, object],
        tool_types: Mapping[str, str],
        request_id: str,
    ) -> Tuple[dict, Optional[str]]:
        upstream = self._open_upstream(authorization, upstream_request)
        try:
            return self._stream_anthropic_response(
                upstream,
                request_payload,
                tool_types,
                request_id,
            )
        finally:
            upstream.close()  # type: ignore[attr-defined]

    def _stream_anthropic_response(
        self,
        upstream: object,
        request_payload: Mapping[str, object],
        tool_types: Mapping[str, str],
        request_id: str,
    ) -> Tuple[dict, Optional[str]]:
        translator = AnthropicStreamTranslator(request_payload, tool_types)
        upstream_events = iter(_iter_anthropic_sse(
            upstream,
            self.server.max_upstream_response_bytes,
        ))

        # Validate message_start before committing HTTP 200 to the client. HTTP,
        # size, and protocol failures detected here can still use an HTTP status.
        for event_type, event in upstream_events:
            translator.consume(event_type, event)
            if translator.message_started:
                break
        if not translator.message_started:
            raise UpstreamResponseError("upstream stream ended before message_start")

        if not self._begin_stream(request_id):
            return translator.anthropic_summary, "client_disconnected"
        for event_type, event in translator.start_events():
            if not self._write_sse_event(event_type, event):
                return translator.anthropic_summary, "client_disconnected"

        try:
            for event_type, event in upstream_events:
                for response_event_type, response_event in translator.consume(
                    event_type,
                    event,
                ):
                    if not self._write_sse_event(response_event_type, response_event):
                        return translator.anthropic_summary, "client_disconnected"
                if translator.completed:
                    return translator.anthropic_summary, None
            translator.finish()
        except UpstreamStreamLimitError as error:
            return self._fail_started_stream(
                translator,
                "upstream_stream_too_large",
                str(error),
            )
        except UpstreamStreamError as error:
            return self._fail_started_stream(
                translator,
                "upstream_stream_error",
                f"{error.code}: {error.message}",
                error.code,
            )
        except UpstreamResponseError as error:
            return self._fail_started_stream(
                translator,
                "upstream_invalid_stream",
                str(error),
            )
        except (
            TimeoutError,
            urllib.error.URLError,
            ConnectionError,
            http.client.HTTPException,
            OSError,
        ) as error:
            return self._fail_started_stream(
                translator,
                "upstream_stream_unavailable",
                str(error),
            )
        raise AssertionError("completed Anthropic stream did not return")

    def _fail_started_stream(
        self,
        translator: AnthropicStreamTranslator,
        category: str,
        message: str,
        response_error_code: str = "server_error",
    ) -> Tuple[dict, str]:
        for event_type, event in translator.failure_events(response_error_code, message):
            if not self._write_sse_event(event_type, event):
                break
        return translator.anthropic_summary, category

    def _begin_stream(self, request_id: str) -> bool:
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Request-Id", request_id)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return False
        return True

    def _write_sse_event(
        self,
        event_type: str,
        event: Mapping[str, object],
    ) -> bool:
        try:
            data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            self.wfile.write(f"event: {event_type}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return False
        return True

    def _stream(self, response: Mapping[str, object], request_id: str) -> bool:
        if not self._begin_stream(request_id):
            return False
        for event_type, event in response_events(response):
            if not self._write_sse_event(event_type, event):
                return False
        return True

    def _audit_request(
        self,
        *,
        request_id: str,
        payload: Optional[Mapping[str, object]],
        upstream_request: Optional[Mapping[str, object]],
        anthropic: Optional[Mapping[str, object]],
        status: int,
        error_category: Optional[str],
        started: float,
    ) -> None:
        audit_log = self.server.audit_log
        if audit_log is None:
            return
        model = payload.get("model") if payload is not None else None
        if not isinstance(model, str):
            model = None
        requested_tokens = payload.get("max_output_tokens") if payload is not None else None
        if not isinstance(requested_tokens, int) or isinstance(requested_tokens, bool):
            requested_tokens = None
        applied_tokens = (
            upstream_request.get("max_tokens") if upstream_request is not None else None
        )
        if not isinstance(applied_tokens, int) or isinstance(applied_tokens, bool):
            applied_tokens = None
        record = {
            "request_id": request_id,
            "model": model,
            "requested_max_output_tokens": requested_tokens,
            "applied_max_output_tokens": applied_tokens,
            "status": status,
            "error_category": error_category,
            "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
            "upstream_usage": _audit_upstream_usage(anthropic),
        }
        try:
            audit_log.emit(record)
        except (OSError, ValueError) as error:
            self.log_error("cannot write audit log: %s", error)


def _read_upstream_body(upstream: object, limit: int) -> bytes:
    body = upstream.read(limit + 1)  # type: ignore[attr-defined]
    if len(body) > limit:
        raise UpstreamResponseError(
            f"upstream response exceeds the {limit}-byte limit"
        )
    return body


def _iter_anthropic_sse(
    upstream: object,
    limit: int,
) -> Iterator[Tuple[str, dict]]:
    """Read Anthropic SSE frames while enforcing one cumulative byte limit."""
    total = 0
    event_name: Optional[str] = None
    data_lines = []
    while True:
        remaining = limit - total
        line = upstream.readline(remaining + 1)  # type: ignore[attr-defined]
        if not isinstance(line, bytes):
            raise UpstreamResponseError("upstream stream returned a non-byte line")
        if not line:
            if data_lines:
                yield _parse_anthropic_sse_event(event_name, data_lines)
            return
        total += len(line)
        if total > limit:
            raise UpstreamStreamLimitError(
                f"upstream stream exceeds the {limit}-byte limit"
            )
        try:
            decoded = line.decode("utf-8")
        except UnicodeDecodeError as error:
            raise UpstreamResponseError(
                f"upstream stream contains invalid UTF-8: {error}"
            ) from error
        decoded = decoded.rstrip("\r\n")
        if not decoded:
            if data_lines:
                yield _parse_anthropic_sse_event(event_name, data_lines)
            event_name = None
            data_lines = []
            continue
        if decoded.startswith(":"):
            continue
        field, separator, value = decoded.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)


def _parse_anthropic_sse_event(
    event_name: Optional[str],
    data_lines: list,
) -> Tuple[str, dict]:
    raw_data = "\n".join(data_lines)
    try:
        event = json.loads(raw_data)
    except json.JSONDecodeError as error:
        raise UpstreamResponseError(
            f"upstream stream contains invalid event JSON: {error}"
        ) from error
    if not isinstance(event, dict):
        raise UpstreamResponseError("upstream stream event data must be an object")
    data_type = event.get("type")
    if not isinstance(data_type, str) or not data_type:
        raise UpstreamResponseError("upstream stream event requires a type")
    if event_name is not None and event_name != data_type:
        raise UpstreamResponseError(
            f"upstream SSE event name {event_name} does not match data type {data_type}"
        )
    return data_type, event


def _audit_upstream_usage(
    anthropic: Optional[Mapping[str, object]],
) -> dict:
    if anthropic is None:
        return {}
    raw = anthropic.get("usage")
    if not isinstance(raw, dict):
        return {}
    usage = {}
    for field in UPSTREAM_USAGE_FIELDS:
        value = raw.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            usage[field] = value
    return usage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Translate Codex Responses requests to Anthropic Messages."
    )
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18766)
    parser.add_argument(
        "--upstream-base-url",
        required=True,
    )
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--max-output-tokens", type=int, default=16384)
    parser.add_argument(
        "--service-id",
        required=True,
        help="stable non-secret provider ID exposed by /health",
    )
    parser.add_argument(
        "--max-upstream-response-bytes",
        type=int,
        default=MAX_UPSTREAM_RESPONSE_BYTES,
    )
    parser.add_argument(
        "--audit-log",
        type=Path,
        default=None,
        help="append prompt- and credential-free JSON audit records to this file",
    )
    parser.add_argument(
        "--model-catalog",
        type=Path,
        required=True,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.listen not in ("127.0.0.1", "::1", "localhost"):
        print("error: adapter must listen on loopback", file=sys.stderr)
        return 2
    if not 1 <= args.port <= 65535:
        print("error: port must be between 1 and 65535", file=sys.stderr)
        return 2
    if not 1 <= args.max_output_tokens <= 64000:
        print("error: max output tokens must be between 1 and 64000", file=sys.stderr)
        return 2
    if args.max_upstream_response_bytes <= 0:
        print("error: max upstream response bytes must be positive", file=sys.stderr)
        return 2
    try:
        normalized_upstream_url = normalize_upstream_base_url(args.upstream_base_url)
        service_fingerprint(args.service_id, normalized_upstream_url)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        model_catalog = json.loads(args.model_catalog.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"error: cannot read model catalog: {error}", file=sys.stderr)
        return 2
    if not isinstance(model_catalog, dict) or not isinstance(model_catalog.get("models"), list):
        print("error: model catalog must contain a models array", file=sys.stderr)
        return 2
    try:
        audit_log = JsonAuditLog(args.audit_log.expanduser()) if args.audit_log else None
    except OSError as error:
        print(f"error: cannot open audit log: {error}", file=sys.stderr)
        return 2
    server = AnthropicAdapterServer(
        (args.listen, args.port),
        args.upstream_base_url,
        args.timeout,
        model_catalog,
        args.max_output_tokens,
        args.service_id,
        args.max_upstream_response_bytes,
        audit_log,
    )
    print(
        f"anthropic-adapter: listening on http://{args.listen}:{args.port}; "
        f"upstream {args.upstream_base_url}",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
