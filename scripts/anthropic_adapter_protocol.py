"""Translate the Codex Responses subset to Anthropic Messages and back."""

from __future__ import annotations

import copy
import json
import time
import uuid
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple


class ProtocolError(ValueError):
    pass


class UpstreamResponseError(ValueError):
    """The upstream returned a successful HTTP response with an invalid body."""

    pass


class UpstreamStreamError(UpstreamResponseError):
    """Anthropic emitted an error event inside an otherwise successful stream."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


FILTERED_FUNCTION_TOOLS = frozenset({"request_user_input", "view_image"})


def _text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ProtocolError("message content must be a string or an array")
    parts: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            raise ProtocolError("message content blocks must be objects")
        block_type = block.get("type")
        if block_type in ("input_text", "output_text", "text"):
            text = block.get("text")
            if not isinstance(text, str):
                raise ProtocolError(f"{block_type}.text must be a string")
            parts.append(text)
            continue
        raise ProtocolError(f"unsupported message content block: {block_type}")
    return "\n".join(parts)


def _text_from_agent_message(content: object) -> str:
    """Extract the visible inter-agent envelope without forwarding Codex ciphertext."""
    if isinstance(content, str):
        if not content:
            raise ProtocolError("agent_message content must not be empty")
        return content
    if not isinstance(content, list):
        raise ProtocolError("agent_message content must be a string or an array")
    parts: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            raise ProtocolError("agent_message content blocks must be objects")
        block_type = block.get("type")
        if block_type in ("input_text", "text"):
            text = block.get("text")
            if not isinstance(text, str):
                raise ProtocolError(f"agent_message {block_type}.text must be a string")
            if text:
                parts.append(text)
            continue
        if block_type == "encrypted_content":
            continue
        raise ProtocolError(f"unsupported agent_message content block: {block_type}")
    if not parts:
        raise ProtocolError("agent_message requires visible input_text content")
    return "\n".join(parts)


def _tool_output_text(output: object) -> str:
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False, separators=(",", ":"))


def _append_message(messages: List[dict], role: str, content: object) -> None:
    blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
    else:
        messages.append({"role": role, "content": list(blocks)})


def _convert_input(input_value: object) -> Tuple[List[str], List[dict]]:
    if isinstance(input_value, str):
        return [], [{"role": "user", "content": [{"type": "text", "text": input_value}]}]
    if not isinstance(input_value, list):
        raise ProtocolError("input must be a string or an array")

    system_parts: List[str] = []
    messages: List[dict] = []
    for item in input_value:
        if not isinstance(item, dict):
            raise ProtocolError("input items must be objects")
        item_type = item.get("type")
        if item_type == "agent_message":
            text = _text_from_agent_message(item.get("content", []))
            _append_message(messages, "user", [{"type": "text", "text": text}])
            continue
        if item_type == "message":
            role = item.get("role")
            text = _text_from_content(item.get("content", ""))
            if role in ("developer", "system"):
                system_parts.append(text)
            elif role == "user":
                _append_message(messages, "user", [{"type": "text", "text": text}])
            elif role == "assistant":
                _append_message(messages, "assistant", [{"type": "text", "text": text}])
            else:
                raise ProtocolError(f"unsupported message role: {role}")
            continue
        if item_type in ("function_call", "custom_tool_call"):
            call_id = item.get("call_id")
            name = item.get("name")
            if not isinstance(call_id, str) or not isinstance(name, str):
                raise ProtocolError(f"{item_type} requires call_id and name")
            if item_type == "function_call":
                arguments = item.get("arguments", "{}")
                if not isinstance(arguments, str):
                    raise ProtocolError("function_call.arguments must be a JSON string")
                try:
                    tool_input = json.loads(arguments)
                except json.JSONDecodeError as error:
                    raise ProtocolError(f"invalid function_call arguments: {error}") from error
            else:
                tool_input = {"input": item.get("input", "")}
            _append_message(
                messages,
                "assistant",
                [{"type": "tool_use", "id": call_id, "name": name, "input": tool_input}],
            )
            continue
        if item_type in ("function_call_output", "custom_tool_call_output"):
            call_id = item.get("call_id")
            if not isinstance(call_id, str):
                raise ProtocolError(f"{item_type} requires call_id")
            _append_message(
                messages,
                "user",
                [{
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": _tool_output_text(item.get("output", "")),
                }],
            )
            continue
        if item_type == "reasoning":
            continue
        raise ProtocolError(f"unsupported Responses input item: {item_type}")
    return system_parts, messages


def _convert_tools(tools: object) -> Tuple[List[dict], Dict[str, str]]:
    if tools is None:
        return [], {}
    if not isinstance(tools, list):
        raise ProtocolError("tools must be an array")
    converted: List[dict] = []
    tool_types: Dict[str, str] = {}
    for tool in tools:
        if not isinstance(tool, dict):
            raise ProtocolError("tool definitions must be objects")
        tool_type = tool.get("type")
        name = tool.get("name")
        if tool_type == "function":
            if not isinstance(name, str):
                raise ProtocolError("function tool requires a name")
            if name in FILTERED_FUNCTION_TOOLS:
                continue
            parameters = tool.get("parameters", {"type": "object", "properties": {}})
            if not isinstance(parameters, dict):
                raise ProtocolError(f"function tool {name} parameters must be an object")
            converted.append({
                "name": name,
                "description": str(tool.get("description", "")),
                "input_schema": parameters,
            })
            tool_types[name] = "function"
            continue
        if tool_type == "custom":
            if not isinstance(name, str):
                raise ProtocolError("custom tool requires a name")
            converted.append({
                "name": name,
                "description": str(tool.get("description", "")),
                "input_schema": {
                    "type": "object",
                    "properties": {"input": {"type": "string"}},
                    "required": ["input"],
                    "additionalProperties": False,
                },
            })
            tool_types[name] = "custom"
            continue
        if tool_type in ("namespace", "web_search"):
            continue
        raise ProtocolError(
            f"unsupported tool type {tool_type}; disable built-in and namespace tools for this worker"
        )
    return converted, tool_types


def _convert_tool_choice(value: object, parallel: bool) -> Optional[dict]:
    if value in (None, "auto"):
        return {"type": "auto", "disable_parallel_tool_use": not parallel}
    if value == "required":
        return {"type": "any", "disable_parallel_tool_use": not parallel}
    if value == "none":
        return None
    if isinstance(value, dict) and value.get("type") == "function":
        name = value.get("name")
        if isinstance(name, str):
            return {"type": "tool", "name": name, "disable_parallel_tool_use": not parallel}
    raise ProtocolError(f"unsupported tool_choice: {value}")


def build_anthropic_request(
    payload: Mapping[str, object],
    max_upstream_output_tokens: int = 16384,
) -> Tuple[dict, Dict[str, str]]:
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise ProtocolError("model must be a non-empty string")
    system_parts, messages = _convert_input(payload.get("input", []))
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions:
        system_parts.insert(0, instructions)
    elif instructions is not None:
        raise ProtocolError("instructions must be a string")
    if not messages:
        raise ProtocolError("at least one user or assistant message is required")

    tools, tool_types = _convert_tools(payload.get("tools", []))
    parallel = bool(payload.get("parallel_tool_calls", True))
    tool_choice = _convert_tool_choice(payload.get("tool_choice", "auto"), parallel)
    max_tokens = payload.get("max_output_tokens", 16384)
    if max_tokens is None:
        max_tokens = 16384
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        raise ProtocolError("max_output_tokens must be a positive integer")

    request = {
        "model": model,
        "max_tokens": min(max_tokens, max_upstream_output_tokens, 64000),
        "messages": messages,
        "stream": payload.get("stream") is not False,
    }
    if system_parts:
        request["system"] = "\n\n".join(part for part in system_parts if part)
    if tools and payload.get("tool_choice") != "none":
        request["tools"] = tools
        if tool_choice is not None:
            request["tool_choice"] = tool_choice
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if effort in ("low", "medium", "high", "max"):
            request["output_config"] = {"effort": effort}
        elif effort in ("xhigh", "ultra"):
            request["output_config"] = {"effort": "max"}
    return request, tool_types


def _usage_token_count(usage: Mapping[str, object], field: str) -> int:
    value = usage.get(field, 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise UpstreamResponseError(f"upstream usage.{field} must be a non-negative integer")
    return value


def _usage(anthropic: Mapping[str, object]) -> dict:
    raw = anthropic.get("usage")
    if raw is None:
        usage: Mapping[str, object] = {}
    elif isinstance(raw, dict):
        usage = raw
    else:
        raise UpstreamResponseError("upstream usage must be an object")
    input_tokens = _usage_token_count(usage, "input_tokens")
    output_tokens = _usage_token_count(usage, "output_tokens")
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {
            "cached_tokens": _usage_token_count(usage, "cache_read_input_tokens")
        },
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": input_tokens + output_tokens,
    }


def build_responses_response(
    anthropic: Mapping[str, object],
    request: Mapping[str, object],
    tool_types: Mapping[str, str],
) -> dict:
    content = anthropic.get("content")
    if not isinstance(content, list):
        raise UpstreamResponseError("upstream response content must be an array")
    upstream_model = anthropic.get("model")
    if upstream_model is not None and (
        not isinstance(upstream_model, str) or not upstream_model
    ):
        raise UpstreamResponseError("upstream response model must be a non-empty string")
    output: List[dict] = []
    for block in content:
        if not isinstance(block, dict):
            raise UpstreamResponseError("upstream response blocks must be objects")
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise UpstreamResponseError("upstream text block requires text")
            if not text.strip():
                continue
            output.append({
                "id": "msg_" + uuid.uuid4().hex,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            })
            continue
        if block_type == "tool_use":
            call_id = block.get("id")
            name = block.get("name")
            tool_input = block.get("input", {})
            if not isinstance(call_id, str) or not isinstance(name, str):
                raise UpstreamResponseError("upstream tool_use requires id and name")
            if not isinstance(tool_input, dict):
                raise UpstreamResponseError("upstream tool_use input must be an object")
            original_type = tool_types.get(name)
            if original_type is None:
                raise UpstreamResponseError(
                    f"upstream called undeclared or filtered tool: {name}"
                )
            if original_type == "custom":
                custom_input = tool_input.get("input", "") if isinstance(tool_input, dict) else ""
                output.append({
                    "id": "ctc_" + uuid.uuid4().hex,
                    "type": "custom_tool_call",
                    "status": "completed",
                    "call_id": call_id,
                    "name": name,
                    "input": custom_input if isinstance(custom_input, str) else json.dumps(custom_input),
                })
            else:
                output.append({
                    "id": "fc_" + uuid.uuid4().hex,
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call_id,
                    "name": name,
                    "arguments": json.dumps(tool_input, ensure_ascii=False, separators=(",", ":")),
                })
            continue
        if block_type == "thinking":
            continue
        raise UpstreamResponseError(f"unsupported upstream response block: {block_type}")

    if not output:
        raise UpstreamResponseError("upstream returned no visible text or tool calls")

    now = int(time.time())
    return {
        "id": "resp_" + uuid.uuid4().hex,
        "object": "response",
        "created_at": now,
        "completed_at": now,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": request.get("instructions"),
        "max_output_tokens": request.get("max_output_tokens"),
        "model": anthropic.get("model", request.get("model")),
        "output": output,
        "parallel_tool_calls": bool(request.get("parallel_tool_calls", True)),
        "previous_response_id": request.get("previous_response_id"),
        "reasoning": request.get("reasoning"),
        "store": bool(request.get("store", False)),
        "temperature": request.get("temperature"),
        "text": request.get("text", {"format": {"type": "text"}}),
        "tool_choice": request.get("tool_choice", "auto"),
        "tools": request.get("tools", []),
        "top_p": request.get("top_p"),
        "truncation": request.get("truncation", "disabled"),
        "usage": _usage(anthropic),
        "metadata": request.get("metadata", {}),
    }


ANTHROPIC_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _skip_json_whitespace(value: str, offset: int) -> int:
    while offset < len(value) and value[offset] in " \t\r\n":
        offset += 1
    return offset


def _json_string_prefix(value: str, offset: int) -> Tuple[str, int, bool]:
    """Decode the stable prefix of a possibly incomplete JSON string."""
    if offset >= len(value):
        return "", offset, False
    if value[offset] != '"':
        raise UpstreamResponseError("custom tool input must be a JSON string")
    decoded: List[str] = []
    offset += 1
    escapes = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }
    while offset < len(value):
        character = value[offset]
        if character == '"':
            return "".join(decoded), offset + 1, True
        if ord(character) < 0x20:
            raise UpstreamResponseError("custom tool input contains invalid JSON")
        if character != "\\":
            decoded.append(character)
            offset += 1
            continue
        if offset + 1 >= len(value):
            return "".join(decoded), offset, False
        escape = value[offset + 1]
        if escape in escapes:
            decoded.append(escapes[escape])
            offset += 2
            continue
        if escape != "u":
            raise UpstreamResponseError("custom tool input contains an invalid JSON escape")
        if offset + 6 > len(value):
            return "".join(decoded), offset, False
        digits = value[offset + 2:offset + 6]
        try:
            codepoint = int(digits, 16)
        except ValueError as error:
            raise UpstreamResponseError(
                "custom tool input contains an invalid Unicode escape"
            ) from error
        if 0xD800 <= codepoint <= 0xDBFF:
            if offset + 12 > len(value):
                return "".join(decoded), offset, False
            if value[offset + 6:offset + 8] != "\\u":
                raise UpstreamResponseError(
                    "custom tool input contains an unpaired Unicode surrogate"
                )
            low_digits = value[offset + 8:offset + 12]
            try:
                low = int(low_digits, 16)
            except ValueError as error:
                raise UpstreamResponseError(
                    "custom tool input contains an invalid Unicode escape"
                ) from error
            if not 0xDC00 <= low <= 0xDFFF:
                raise UpstreamResponseError(
                    "custom tool input contains an unpaired Unicode surrogate"
                )
            decoded.append(chr(0x10000 + ((codepoint - 0xD800) << 10) + low - 0xDC00))
            offset += 12
            continue
        if 0xDC00 <= codepoint <= 0xDFFF:
            raise UpstreamResponseError(
                "custom tool input contains an unpaired Unicode surrogate"
            )
        decoded.append(chr(codepoint))
        offset += 6
    return "".join(decoded), offset, False


def _custom_tool_input_prefix(value: str) -> Tuple[str, bool]:
    """Extract a decoded prefix from the adapter's {"input": string} envelope."""
    offset = _skip_json_whitespace(value, 0)
    if offset >= len(value):
        return "", False
    if value[offset] != "{":
        raise UpstreamResponseError("custom tool input must be a JSON object")
    offset = _skip_json_whitespace(value, offset + 1)
    key, offset, key_complete = _json_string_prefix(value, offset)
    if not key_complete:
        return "", False
    if key != "input":
        raise UpstreamResponseError("custom tool input must contain only the input field")
    offset = _skip_json_whitespace(value, offset)
    if offset >= len(value):
        return "", False
    if value[offset] != ":":
        raise UpstreamResponseError("custom tool input contains invalid JSON")
    offset = _skip_json_whitespace(value, offset + 1)
    decoded, offset, value_complete = _json_string_prefix(value, offset)
    if not value_complete:
        return decoded, False
    offset = _skip_json_whitespace(value, offset)
    if offset >= len(value):
        return decoded, False
    if value[offset] != "}":
        raise UpstreamResponseError("custom tool input must contain only the input field")
    offset = _skip_json_whitespace(value, offset + 1)
    if offset != len(value):
        raise UpstreamResponseError("custom tool input contains trailing JSON data")
    return decoded, True


class _CustomToolInputAccumulator:
    def __init__(self) -> None:
        self.raw = ""
        self.emitted = ""

    def feed(self, partial_json: str) -> str:
        self.raw += partial_json
        decoded, _ = _custom_tool_input_prefix(self.raw)
        if not decoded.startswith(self.emitted):
            raise UpstreamResponseError("custom tool input changed an emitted prefix")
        delta = decoded[len(self.emitted):]
        self.emitted = decoded
        return delta

    def finish(self) -> str:
        try:
            parsed = json.loads(self.raw or "{}")
        except json.JSONDecodeError as error:
            raise UpstreamResponseError(
                f"upstream tool input contains invalid JSON: {error}"
            ) from error
        if (
            not isinstance(parsed, dict)
            or set(parsed) != {"input"}
            or not isinstance(parsed.get("input"), str)
        ):
            raise UpstreamResponseError(
                "custom tool input must be an object containing only a string input field"
            )
        decoded, complete = _custom_tool_input_prefix(self.raw)
        if not complete or decoded != parsed["input"] or decoded != self.emitted:
            raise UpstreamResponseError("custom tool input stream did not assemble correctly")
        return decoded


class AnthropicStreamTranslator:
    """Translate one validated Anthropic Messages event stream into Responses events."""

    def __init__(
        self,
        request: Mapping[str, object],
        tool_types: Mapping[str, str],
    ) -> None:
        now = int(time.time())
        self.response: dict = {
            "id": "resp_" + uuid.uuid4().hex,
            "object": "response",
            "created_at": now,
            "completed_at": None,
            "status": "in_progress",
            "error": None,
            "incomplete_details": None,
            "instructions": request.get("instructions"),
            "max_output_tokens": request.get("max_output_tokens"),
            "model": request.get("model"),
            "output": [],
            "parallel_tool_calls": bool(request.get("parallel_tool_calls", True)),
            "previous_response_id": request.get("previous_response_id"),
            "reasoning": request.get("reasoning"),
            "store": bool(request.get("store", False)),
            "temperature": request.get("temperature"),
            "text": request.get("text", {"format": {"type": "text"}}),
            "tool_choice": request.get("tool_choice", "auto"),
            "tools": request.get("tools", []),
            "top_p": request.get("top_p"),
            "truncation": request.get("truncation", "disabled"),
            "usage": None,
            "metadata": request.get("metadata", {}),
        }
        self.tool_types = dict(tool_types)
        self.outputs: List[dict] = []
        self.blocks: Dict[int, dict] = {}
        self.usage: Dict[str, int] = {}
        self.message_started = False
        self.events_started = False
        self.completed = False
        self.stop_reason: Optional[str] = None
        self._sequence = 0

    @property
    def anthropic_summary(self) -> dict:
        return {
            "model": self.response.get("model"),
            "usage": dict(self.usage),
        }

    def _event(self, event_type: str, **fields: object) -> Tuple[str, dict]:
        event = {"type": event_type, **fields, "sequence_number": self._sequence}
        self._sequence += 1
        return event_type, event

    def start_events(self) -> List[Tuple[str, dict]]:
        if not self.message_started:
            raise UpstreamResponseError("upstream stream did not start with message_start")
        if self.events_started:
            raise UpstreamResponseError("Responses stream was started more than once")
        self.events_started = True
        created = copy.deepcopy(self.response)
        return [
            self._event("response.created", response=created),
            self._event("response.in_progress", response=copy.deepcopy(created)),
        ]

    def consume(
        self,
        event_type: str,
        event: Mapping[str, object],
    ) -> List[Tuple[str, dict]]:
        if event_type == "ping":
            return []
        if event_type == "error":
            raw_error = event.get("error")
            if not isinstance(raw_error, dict):
                raise UpstreamResponseError("upstream error event requires an error object")
            code = raw_error.get("type", "upstream_error")
            message = raw_error.get("message", "upstream stream failed")
            if not isinstance(code, str) or not isinstance(message, str):
                raise UpstreamResponseError("upstream error event is invalid")
            raise UpstreamStreamError(code, message)
        if event_type == "message_start":
            return self._message_start(event)
        if event_type == "content_block_start":
            return self._content_block_start(event)
        if event_type == "content_block_delta":
            return self._content_block_delta(event)
        if event_type == "content_block_stop":
            return self._content_block_stop(event)
        if event_type == "message_delta":
            return self._message_delta(event)
        if event_type == "message_stop":
            return self._message_stop()
        # Anthropic may add event types without a version bump. Unknown events
        # cannot mutate any block we understand, so they are intentionally ignored.
        return []

    def _message_start(self, event: Mapping[str, object]) -> List[Tuple[str, dict]]:
        if self.message_started or self.completed:
            raise UpstreamResponseError("upstream emitted duplicate message_start")
        message = event.get("message")
        if not isinstance(message, dict):
            raise UpstreamResponseError("message_start requires a message object")
        content = message.get("content")
        if not isinstance(content, list) or content:
            raise UpstreamResponseError("message_start content must be an empty array")
        model = message.get("model")
        if not isinstance(model, str) or not model:
            raise UpstreamResponseError("message_start model must be a non-empty string")
        self.response["model"] = model
        self._update_usage(message.get("usage"), "message_start")
        self.message_started = True
        return []

    def _content_block_start(
        self,
        event: Mapping[str, object],
    ) -> List[Tuple[str, dict]]:
        self._require_active_message()
        index = self._block_index(event)
        if index in self.blocks:
            raise UpstreamResponseError(f"duplicate content block index {index}")
        content_block = event.get("content_block")
        if not isinstance(content_block, dict):
            raise UpstreamResponseError("content_block_start requires content_block")
        block_type = content_block.get("type")
        if block_type in ("thinking", "redacted_thinking", "fallback"):
            self.blocks[index] = {"kind": "ignored"}
            return []
        output_index = len(self.outputs)
        if block_type == "text":
            initial = content_block.get("text", "")
            if not isinstance(initial, str):
                raise UpstreamResponseError("upstream text block requires text")
            item = {
                "id": "msg_" + uuid.uuid4().hex,
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [{"type": "output_text", "text": initial, "annotations": []}],
            }
            self.outputs.append(item)
            self.blocks[index] = {
                "kind": "text",
                "item": item,
                "output_index": output_index,
            }
            added = dict(item)
            added["content"] = []
            events = [
                self._event(
                    "response.output_item.added",
                    output_index=output_index,
                    item=added,
                ),
                self._event(
                    "response.content_part.added",
                    item_id=item["id"],
                    output_index=output_index,
                    content_index=0,
                    part={"type": "output_text", "text": "", "annotations": []},
                ),
            ]
            if initial:
                events.append(self._text_delta_event(item, output_index, initial))
            return events
        if block_type != "tool_use":
            raise UpstreamResponseError(f"unsupported upstream response block: {block_type}")
        call_id = content_block.get("id")
        name = content_block.get("name")
        initial_input = content_block.get("input", {})
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            raise UpstreamResponseError("upstream tool_use requires id and name")
        if initial_input != {}:
            raise UpstreamResponseError("streaming tool_use must start with empty input")
        original_type = self.tool_types.get(name)
        if original_type is None:
            raise UpstreamResponseError(
                f"upstream called undeclared or filtered tool: {name}"
            )
        if original_type == "custom":
            item = {
                "id": "ctc_" + uuid.uuid4().hex,
                "type": "custom_tool_call",
                "status": "in_progress",
                "call_id": call_id,
                "name": name,
                "input": "",
            }
            accumulator: object = _CustomToolInputAccumulator()
        else:
            item = {
                "id": "fc_" + uuid.uuid4().hex,
                "type": "function_call",
                "status": "in_progress",
                "call_id": call_id,
                "name": name,
                "arguments": "",
            }
            accumulator = ""
        self.outputs.append(item)
        self.blocks[index] = {
            "kind": original_type,
            "item": item,
            "output_index": output_index,
            "accumulator": accumulator,
        }
        return [
            self._event(
                "response.output_item.added",
                output_index=output_index,
                item=copy.deepcopy(item),
            )
        ]

    def _content_block_delta(
        self,
        event: Mapping[str, object],
    ) -> List[Tuple[str, dict]]:
        self._require_active_message()
        index = self._block_index(event)
        block = self.blocks.get(index)
        if block is None:
            raise UpstreamResponseError(f"delta references unopened content block {index}")
        delta = event.get("delta")
        if not isinstance(delta, dict):
            raise UpstreamResponseError("content_block_delta requires a delta object")
        kind = block["kind"]
        delta_type = delta.get("type")
        if kind == "ignored":
            return []
        item = block["item"]
        output_index = block["output_index"]
        if kind == "text":
            text = delta.get("text")
            if delta_type != "text_delta" or not isinstance(text, str):
                raise UpstreamResponseError("text block received a non-text delta")
            item["content"][0]["text"] += text
            return [self._text_delta_event(item, output_index, text)] if text else []
        partial_json = delta.get("partial_json")
        if delta_type != "input_json_delta" or not isinstance(partial_json, str):
            raise UpstreamResponseError("tool_use block received a non-JSON delta")
        if kind == "custom":
            accumulator = block["accumulator"]
            if not isinstance(accumulator, _CustomToolInputAccumulator):
                raise UpstreamResponseError("custom tool stream state is invalid")
            custom_delta = accumulator.feed(partial_json)
            if not custom_delta:
                return []
            item["input"] += custom_delta
            return [self._event(
                "response.custom_tool_call_input.delta",
                item_id=item["id"],
                output_index=output_index,
                delta=custom_delta,
            )]
        raw = block["accumulator"] + partial_json
        block["accumulator"] = raw
        item["arguments"] = raw
        if not partial_json:
            return []
        return [self._event(
            "response.function_call_arguments.delta",
            item_id=item["id"],
            output_index=output_index,
            delta=partial_json,
        )]

    def _content_block_stop(
        self,
        event: Mapping[str, object],
    ) -> List[Tuple[str, dict]]:
        self._require_active_message()
        index = self._block_index(event)
        block = self.blocks.pop(index, None)
        if block is None:
            raise UpstreamResponseError(f"stop references unopened content block {index}")
        kind = block["kind"]
        if kind == "ignored":
            return []
        item = block["item"]
        output_index = block["output_index"]
        item["status"] = "completed"
        if kind == "text":
            part = item["content"][0]
            events = [
                self._event(
                    "response.output_text.done",
                    item_id=item["id"],
                    output_index=output_index,
                    content_index=0,
                    text=part["text"],
                ),
                self._event(
                    "response.content_part.done",
                    item_id=item["id"],
                    output_index=output_index,
                    content_index=0,
                    part=copy.deepcopy(part),
                ),
            ]
        elif kind == "custom":
            accumulator = block["accumulator"]
            if not isinstance(accumulator, _CustomToolInputAccumulator):
                raise UpstreamResponseError("custom tool stream state is invalid")
            item["input"] = accumulator.finish()
            events = [self._event(
                "response.custom_tool_call_input.done",
                item_id=item["id"],
                output_index=output_index,
                input=item["input"],
            )]
        else:
            arguments = block["accumulator"] or "{}"
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError as error:
                raise UpstreamResponseError(
                    f"upstream tool input contains invalid JSON: {error}"
                ) from error
            if not isinstance(parsed, dict):
                raise UpstreamResponseError("upstream tool input must be a JSON object")
            item["arguments"] = arguments
            events = [self._event(
                "response.function_call_arguments.done",
                item_id=item["id"],
                output_index=output_index,
                arguments=arguments,
            )]
        events.append(self._event(
            "response.output_item.done",
            output_index=output_index,
            item=copy.deepcopy(item),
        ))
        return events

    def _message_delta(self, event: Mapping[str, object]) -> List[Tuple[str, dict]]:
        self._require_active_message()
        if self.blocks:
            raise UpstreamResponseError("message_delta arrived before content blocks stopped")
        delta = event.get("delta")
        if not isinstance(delta, dict):
            raise UpstreamResponseError("message_delta requires a delta object")
        stop_reason = delta.get("stop_reason")
        if stop_reason is not None and not isinstance(stop_reason, str):
            raise UpstreamResponseError("message_delta stop_reason must be a string or null")
        if isinstance(stop_reason, str):
            self.stop_reason = stop_reason
        self._update_usage(event.get("usage"), "message_delta")
        return []

    def _message_stop(self) -> List[Tuple[str, dict]]:
        self._require_active_message()
        if self.blocks:
            raise UpstreamResponseError("message_stop arrived before content blocks stopped")
        has_visible_output = any(
            item.get("type") in ("function_call", "custom_tool_call")
            or (
                item.get("type") == "message"
                and any(
                    isinstance(part, dict)
                    and isinstance(part.get("text"), str)
                    and bool(part["text"].strip())
                    for part in item.get("content", [])
                )
            )
            for item in self.outputs
        )
        if not has_visible_output:
            raise UpstreamResponseError("upstream returned no visible text or tool calls")
        self.completed = True
        self.response.update({
            "completed_at": int(time.time()),
            "status": "completed",
            "output": copy.deepcopy(self.outputs),
            "usage": _usage({"usage": self.usage}),
        })
        return [self._event(
            "response.completed",
            response=copy.deepcopy(self.response),
        )]

    def finish(self) -> None:
        if not self.completed:
            raise UpstreamResponseError("upstream stream ended before message_stop")

    def failure_events(
        self,
        code: str,
        message: str,
    ) -> List[Tuple[str, dict]]:
        failed = copy.deepcopy(self.response)
        failed.update({
            "status": "failed",
            "completed_at": None,
            "error": {"code": code, "message": message},
            "output": [],
            "usage": None,
        })
        return [
            self._event(
                "error",
                code=code,
                message=message,
                param=None,
            ),
            self._event("response.failed", response=failed),
        ]

    def _text_delta_event(
        self,
        item: Mapping[str, object],
        output_index: int,
        text: str,
    ) -> Tuple[str, dict]:
        return self._event(
            "response.output_text.delta",
            item_id=item["id"],
            output_index=output_index,
            content_index=0,
            delta=text,
        )

    def _update_usage(self, raw: object, event_name: str) -> None:
        if raw is None:
            return
        if not isinstance(raw, dict):
            raise UpstreamResponseError(f"{event_name} usage must be an object")
        for field in ANTHROPIC_USAGE_FIELDS:
            if field not in raw:
                continue
            value = raw[field]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise UpstreamResponseError(
                    f"upstream usage.{field} must be a non-negative integer"
                )
            self.usage[field] = value

    def _require_active_message(self) -> None:
        if not self.message_started:
            raise UpstreamResponseError("upstream content arrived before message_start")
        if self.completed:
            raise UpstreamResponseError("upstream content arrived after message_stop")

    @staticmethod
    def _block_index(event: Mapping[str, object]) -> int:
        index = event.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise UpstreamResponseError("content block index must be a non-negative integer")
        return index


def response_events(response: Mapping[str, object]) -> Iterable[Tuple[str, dict]]:
    created = dict(response)
    created.update({"status": "in_progress", "completed_at": None, "output": [], "usage": None})
    yield "response.created", {"type": "response.created", "response": created}
    yield "response.in_progress", {"type": "response.in_progress", "response": created}
    output = response.get("output", [])
    if not isinstance(output, list):
        raise ProtocolError("response output must be an array")
    for output_index, item in enumerate(output):
        if not isinstance(item, dict):
            raise ProtocolError("response output items must be objects")
        item_type = item.get("type")
        added = dict(item)
        added["status"] = "in_progress"
        if item_type == "message":
            added["content"] = []
        elif item_type == "function_call":
            added["arguments"] = ""
        elif item_type == "custom_tool_call":
            added["input"] = ""
        yield "response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": output_index,
            "item": added,
        }
        if item_type == "message":
            content = item.get("content", [])
            if not isinstance(content, list):
                raise ProtocolError("message output content must be an array")
            for content_index, part in enumerate(content):
                if not isinstance(part, dict) or part.get("type") != "output_text":
                    raise ProtocolError("only output_text response parts are supported")
                text = str(part.get("text", ""))
                empty_part = {"type": "output_text", "text": "", "annotations": []}
                yield "response.content_part.added", {
                    "type": "response.content_part.added",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "content_index": content_index,
                    "part": empty_part,
                }
                if text:
                    yield "response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "item_id": item["id"],
                        "output_index": output_index,
                        "content_index": content_index,
                        "delta": text,
                    }
                yield "response.output_text.done", {
                    "type": "response.output_text.done",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "content_index": content_index,
                    "text": text,
                }
                yield "response.content_part.done", {
                    "type": "response.content_part.done",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "content_index": content_index,
                    "part": part,
                }
        elif item_type == "function_call":
            arguments = str(item.get("arguments", ""))
            if arguments:
                yield "response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "delta": arguments,
                }
            yield "response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "item_id": item["id"],
                "output_index": output_index,
                "arguments": arguments,
            }
        elif item_type == "custom_tool_call":
            custom_input = str(item.get("input", ""))
            if custom_input:
                yield "response.custom_tool_call_input.delta", {
                    "type": "response.custom_tool_call_input.delta",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "delta": custom_input,
                }
            yield "response.custom_tool_call_input.done", {
                "type": "response.custom_tool_call_input.done",
                "item_id": item["id"],
                "output_index": output_index,
                "input": custom_input,
            }
        else:
            raise ProtocolError(f"unsupported response output item: {item_type}")
        yield "response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": output_index,
            "item": item,
        }
    yield "response.completed", {"type": "response.completed", "response": dict(response)}
