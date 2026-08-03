from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import anthropic_adapter_protocol as protocol  # noqa: E402


class AnthropicAdapterProtocolTests(unittest.TestCase):
    def test_request_output_tokens_are_capped_by_adapter_policy(self) -> None:
        request, _ = protocol.build_anthropic_request(
            {
                "model": "gemini-3.5-flash",
                "input": "Return a short result.",
                "max_output_tokens": 64000,
            },
            max_upstream_output_tokens=4096,
        )
        self.assertEqual(4096, request["max_tokens"])

    def test_native_agent_message_keeps_visible_envelope_and_drops_ciphertext(self) -> None:
        request, _ = protocol.build_anthropic_request({
            "model": "deepseek-v4-flash",
            "input": [{
                "type": "agent_message",
                "author": "/root",
                "recipient": "/root/subagent_pool_1",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Message Type: NEW_TASK\nTask name: /root/subagent_pool_1\nPayload:\n",
                    },
                    {"type": "encrypted_content", "encrypted_content": "opaque-secret"},
                ],
            }],
        })
        content = request["messages"][0]["content"]
        self.assertEqual("user", request["messages"][0]["role"])
        self.assertEqual("Message Type: NEW_TASK\nTask name: /root/subagent_pool_1\nPayload:\n", content[0]["text"])
        self.assertNotIn("opaque-secret", json.dumps(request))

    def test_native_agent_follow_up_is_converted_to_user_message(self) -> None:
        request, _ = protocol.build_anthropic_request({
            "model": "deepseek-v4-flash",
            "input": [{
                "type": "agent_message",
                "content": "Message Type: MESSAGE\nReread the claimed task.",
            }],
        })
        self.assertEqual(
            "Message Type: MESSAGE\nReread the claimed task.",
            request["messages"][0]["content"][0]["text"],
        )

    def test_native_agent_message_rejects_unknown_visible_blocks(self) -> None:
        with self.assertRaisesRegex(protocol.ProtocolError, "unsupported agent_message"):
            protocol.build_anthropic_request({
                "model": "deepseek-v4-flash",
                "input": [{
                    "type": "agent_message",
                    "content": [{"type": "input_image", "image_url": "ignored"}],
                }],
            })

    def test_native_agent_message_rejects_ciphertext_only(self) -> None:
        with self.assertRaisesRegex(protocol.ProtocolError, "requires visible input_text"):
            protocol.build_anthropic_request({
                "model": "deepseek-v4-flash",
                "input": [{
                    "type": "agent_message",
                    "content": [{"type": "encrypted_content", "encrypted_content": "opaque"}],
                }],
            })

    def test_text_request_preserves_instructions_and_messages(self) -> None:
        request, tool_types = protocol.build_anthropic_request({
            "model": "claude-opus-4-6",
            "stream": False,
            "instructions": "system instructions",
            "input": [
                {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "developer rule"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]},
            ],
            "tools": [],
        })
        self.assertEqual("claude-opus-4-6", request["model"])
        self.assertEqual("system instructions\n\ndeveloper rule", request["system"])
        self.assertEqual([{"type": "text", "text": "hello"}], request["messages"][0]["content"])
        self.assertEqual({}, tool_types)
        self.assertFalse(request["stream"])

    def test_request_streams_upstream_unless_explicitly_disabled(self) -> None:
        streaming, _ = protocol.build_anthropic_request({
            "model": "claude-opus-4-6",
            "input": "hello",
        })
        non_streaming, _ = protocol.build_anthropic_request({
            "model": "claude-opus-4-6",
            "input": "hello",
            "stream": False,
        })
        self.assertTrue(streaming["stream"])
        self.assertFalse(non_streaming["stream"])

    def test_function_tools_and_parallel_choice_are_converted(self) -> None:
        request, tool_types = protocol.build_anthropic_request({
            "model": "claude-opus-4-6",
            "input": "run pwd",
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [{
                "type": "function",
                "name": "exec_command",
                "description": "Run a command",
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                },
            }],
        })
        self.assertEqual({"exec_command": "function"}, tool_types)
        self.assertEqual("exec_command", request["tools"][0]["name"])
        self.assertEqual(
            {"type": "auto", "disable_parallel_tool_use": True},
            request["tool_choice"],
        )

    def test_reasoning_effort_is_forwarded_without_thinking_state(self) -> None:
        request, _ = protocol.build_anthropic_request({
            "model": "claude-opus-4-6",
            "input": "analyze",
            "reasoning": {"effort": "high", "summary": "none"},
        })
        self.assertEqual({"effort": "high"}, request["output_config"])
        self.assertNotIn("thinking", request)

    def test_custom_apply_patch_tool_is_wrapped(self) -> None:
        request, tool_types = protocol.build_anthropic_request({
            "model": "claude-opus-4-6",
            "input": "edit the file",
            "tools": [{"type": "custom", "name": "apply_patch", "description": "Apply patch"}],
        })
        self.assertEqual({"apply_patch": "custom"}, tool_types)
        schema = request["tools"][0]["input_schema"]
        self.assertEqual(["input"], schema["required"])

    def test_previous_tool_calls_and_outputs_become_anthropic_blocks(self) -> None:
        request, _ = protocol.build_anthropic_request({
            "model": "claude-opus-4-6",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "exec_command",
                    "arguments": '{"cmd":"pwd"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "/tmp/project",
                },
            ],
            "tools": [],
        })
        self.assertEqual("assistant", request["messages"][0]["role"])
        self.assertEqual("tool_use", request["messages"][0]["content"][0]["type"])
        self.assertEqual("user", request["messages"][1]["role"])
        self.assertEqual("tool_result", request["messages"][1]["content"][0]["type"])

    def test_anthropic_text_and_function_call_become_response_items(self) -> None:
        response = protocol.build_responses_response(
            {
                "model": "claude-opus-4-6",
                "content": [
                    {"type": "text", "text": "checking"},
                    {
                        "type": "tool_use",
                        "id": "tool_1",
                        "name": "exec_command",
                        "input": {"cmd": "pwd"},
                    },
                ],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
            {"model": "claude-opus-4-6", "tools": []},
            {"exec_command": "function"},
        )
        self.assertEqual("message", response["output"][0]["type"])
        call = response["output"][1]
        self.assertEqual("function_call", call["type"])
        self.assertEqual("tool_1", call["call_id"])
        self.assertEqual({"cmd": "pwd"}, json.loads(call["arguments"]))
        self.assertEqual(14, response["usage"]["total_tokens"])

    def test_whitespace_only_upstream_response_is_rejected(self) -> None:
        with self.assertRaisesRegex(protocol.UpstreamResponseError, "no visible text"):
            protocol.build_responses_response(
                {
                    "model": "gemini-3.5-flash",
                    "content": [{"type": "text", "text": " \n\t"}],
                    "usage": {"input_tokens": 10, "output_tokens": 4096},
                },
                {"model": "gemini-3.5-flash", "tools": []},
                {},
            )

    def test_invalid_upstream_structure_is_not_a_client_protocol_error(self) -> None:
        self.assertFalse(issubclass(protocol.UpstreamResponseError, protocol.ProtocolError))
        with self.assertRaisesRegex(protocol.UpstreamResponseError, "content must be an array"):
            protocol.build_responses_response(
                {"content": "invalid"},
                {"model": "gemini-3.5-flash"},
                {},
            )

    def test_invalid_upstream_usage_is_rejected(self) -> None:
        with self.assertRaisesRegex(protocol.UpstreamResponseError, "usage.output_tokens"):
            protocol.build_responses_response(
                {
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"input_tokens": 1, "output_tokens": "many"},
                },
                {"model": "gemini-3.5-flash"},
                {},
            )

    def test_whitespace_is_dropped_when_response_contains_tool_call(self) -> None:
        response = protocol.build_responses_response(
            {
                "model": "gemini-3.5-flash",
                "content": [
                    {"type": "text", "text": "   "},
                    {
                        "type": "tool_use",
                        "id": "tool_1",
                        "name": "exec_command",
                        "input": {"cmd": "pwd"},
                    },
                ],
            },
            {"model": "gemini-3.5-flash", "tools": []},
            {"exec_command": "function"},
        )
        self.assertEqual(["function_call"], [item["type"] for item in response["output"]])

    def test_custom_tool_use_becomes_custom_tool_call(self) -> None:
        response = protocol.build_responses_response(
            {
                "model": "claude-opus-4-6",
                "content": [{
                    "type": "tool_use",
                    "id": "tool_patch",
                    "name": "apply_patch",
                    "input": {"input": "*** Begin Patch"},
                }],
                "usage": {},
            },
            {"model": "claude-opus-4-6"},
            {"apply_patch": "custom"},
        )
        item = response["output"][0]
        self.assertEqual("custom_tool_call", item["type"])
        self.assertEqual("*** Begin Patch", item["input"])

    def test_stream_always_terminates_with_response_completed(self) -> None:
        response = protocol.build_responses_response(
            {
                "model": "claude-opus-4-6",
                "content": [{"type": "text", "text": "done"}],
                "usage": {},
            },
            {"model": "claude-opus-4-6"},
            {},
        )
        events = list(protocol.response_events(response))
        self.assertEqual("response.created", events[0][0])
        self.assertEqual("response.completed", events[-1][0])
        self.assertEqual(response["id"], events[-1][1]["response"]["id"])

    def test_true_stream_translates_text_deltas_and_cumulative_usage(self) -> None:
        translator = protocol.AnthropicStreamTranslator(
            {"model": "requested-model"},
            {},
        )
        events = []
        events.extend(translator.consume("message_start", {
            "type": "message_start",
            "message": {
                "model": "resolved-model",
                "content": [],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 2,
                },
            },
        }))
        events.extend(translator.start_events())
        events.extend(translator.consume("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }))
        for text in ("Hel", "lo"):
            events.extend(translator.consume("content_block_delta", {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            }))
        events.extend(translator.consume("content_block_stop", {
            "type": "content_block_stop",
            "index": 0,
        }))
        events.extend(translator.consume("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 4},
        }))
        events.extend(translator.consume("message_stop", {"type": "message_stop"}))

        event_types = [event_type for event_type, _ in events]
        self.assertEqual(
            [
                "response.created",
                "response.in_progress",
                "response.output_item.added",
                "response.content_part.added",
                "response.output_text.delta",
                "response.output_text.delta",
                "response.output_text.done",
                "response.content_part.done",
                "response.output_item.done",
                "response.completed",
            ],
            event_types,
        )
        self.assertEqual(
            list(range(len(events))),
            [event["sequence_number"] for _, event in events],
        )
        completed = events[-1][1]["response"]
        self.assertEqual("resolved-model", completed["model"])
        self.assertEqual("Hello", completed["output"][0]["content"][0]["text"])
        self.assertEqual(10, completed["usage"]["input_tokens"])
        self.assertEqual(4, completed["usage"]["output_tokens"])
        self.assertEqual(2, completed["usage"]["input_tokens_details"]["cached_tokens"])
        self.assertEqual(14, completed["usage"]["total_tokens"])

    def test_true_stream_assembles_and_validates_function_tool_json(self) -> None:
        translator = protocol.AnthropicStreamTranslator(
            {"model": "model"},
            {"exec_command": "function"},
        )
        translator.consume("message_start", {
            "type": "message_start",
            "message": {"model": "model", "content": [], "usage": {}},
        })
        translator.start_events()
        events = translator.consume("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "tool_1",
                "name": "exec_command",
                "input": {},
            },
        })
        for partial in ('{"cmd":', '"pwd"}'):
            events.extend(translator.consume("content_block_delta", {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": partial},
            }))
        events.extend(translator.consume("content_block_stop", {
            "type": "content_block_stop",
            "index": 0,
        }))
        deltas = [
            event["delta"]
            for event_type, event in events
            if event_type == "response.function_call_arguments.delta"
        ]
        self.assertEqual('{"cmd":"pwd"}', "".join(deltas))
        done = next(
            event for event_type, event in events
            if event_type == "response.function_call_arguments.done"
        )
        self.assertEqual({"cmd": "pwd"}, json.loads(done["arguments"]))

    def test_true_stream_decodes_custom_tool_input_incrementally(self) -> None:
        translator = protocol.AnthropicStreamTranslator(
            {"model": "model"},
            {"apply_patch": "custom"},
        )
        translator.consume("message_start", {
            "type": "message_start",
            "message": {"model": "model", "content": [], "usage": {}},
        })
        translator.start_events()
        events = translator.consume("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "tool_patch",
                "name": "apply_patch",
                "input": {},
            },
        })
        for partial in (
            '{"input":"line 1\\n',
            'line 2 \\uD83D',
            '\\uDE00"}',
        ):
            events.extend(translator.consume("content_block_delta", {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": partial},
            }))
        events.extend(translator.consume("content_block_stop", {
            "type": "content_block_stop",
            "index": 0,
        }))
        deltas = [
            event["delta"]
            for event_type, event in events
            if event_type == "response.custom_tool_call_input.delta"
        ]
        self.assertEqual("line 1\nline 2 😀", "".join(deltas))
        done = next(
            event for event_type, event in events
            if event_type == "response.custom_tool_call_input.done"
        )
        self.assertEqual("line 1\nline 2 😀", done["input"])

    def test_true_stream_rejects_invalid_tool_json_at_block_stop(self) -> None:
        translator = protocol.AnthropicStreamTranslator(
            {"model": "model"},
            {"exec_command": "function"},
        )
        translator.consume("message_start", {
            "type": "message_start",
            "message": {"model": "model", "content": [], "usage": {}},
        })
        translator.start_events()
        translator.consume("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "tool_1",
                "name": "exec_command",
                "input": {},
            },
        })
        translator.consume("content_block_delta", {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"cmd":'},
        })
        with self.assertRaisesRegex(protocol.UpstreamResponseError, "invalid JSON"):
            translator.consume("content_block_stop", {
                "type": "content_block_stop",
                "index": 0,
            })

    def test_upstream_cannot_call_undeclared_or_filtered_tools(self) -> None:
        anthropic = {
            "model": "model",
            "content": [{
                "type": "tool_use",
                "id": "tool_1",
                "name": "request_user_input",
                "input": {"question": "bypass"},
            }],
        }
        with self.assertRaisesRegex(
            protocol.UpstreamResponseError,
            "undeclared or filtered tool",
        ):
            protocol.build_responses_response(
                anthropic,
                {"model": "model"},
                {},
            )

        translator = protocol.AnthropicStreamTranslator({"model": "model"}, {})
        translator.consume("message_start", {
            "type": "message_start",
            "message": {"model": "model", "content": [], "usage": {}},
        })
        translator.start_events()
        with self.assertRaisesRegex(
            protocol.UpstreamResponseError,
            "undeclared or filtered tool",
        ):
            translator.consume("content_block_start", {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool_1",
                    "name": "view_image",
                    "input": {},
                },
            })

    def test_namespace_and_builtin_tools_are_filtered(self) -> None:
        for tool in (
            {"type": "namespace", "name": "multi_agent_v1", "tools": []},
            {"type": "web_search"},
        ):
            with self.subTest(tool=tool):
                request, tool_types = protocol.build_anthropic_request({
                    "model": "claude-opus-4-6",
                    "input": "hello",
                    "tools": [tool],
                })
                self.assertNotIn("tools", request)
                self.assertEqual({}, tool_types)

    def test_worker_incompatible_function_tools_are_filtered(self) -> None:
        request, tool_types = protocol.build_anthropic_request({
            "model": "claude-opus-4-6",
            "input": "hello",
            "tools": [
                {"type": "function", "name": "view_image", "parameters": {"type": "object"}},
                {"type": "function", "name": "request_user_input", "parameters": {"type": "object"}},
                {"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
            ],
        })
        self.assertEqual(["exec_command"], [tool["name"] for tool in request["tools"]])
        self.assertEqual({"exec_command": "function"}, tool_types)


if __name__ == "__main__":
    unittest.main()
