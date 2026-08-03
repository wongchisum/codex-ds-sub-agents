# Providers and protocol adapters

English · [简体中文](zh-CN/MODEL_ADAPTERS.md)

## Manifest shape

A manifest contains `providers`, `models`, and `selection`:

```json
{
  "schema_version": 2,
  "selection": {
    "primary": "model-a",
    "fallbacks": ["model-b"],
    "max_switches": 1
  },
  "providers": [],
  "models": []
}
```

A provider declares connection and authentication. A model declares the remote model, Codex agent, and context metadata. Selection determines the candidate order for subagent batches.

`model.id` is a local selection key such as `gemini-3-5-flash`; `remote_model` is the exact upstream name such as `gemini-3.5-flash`.

## Provider

```json
{
  "id": "example_provider",
  "name": "Example Provider",
  "base_url": "https://example.com/api",
  "protocol": "responses",
  "upstream_protocol": "openai_responses",
  "auth": {
    "type": "keychain",
    "service": "example-api-key",
    "account": "codex"
  },
  "request_max_retries": 2,
  "stream_max_retries": 2,
  "stream_idle_timeout_ms": 300000
}
```

Rules:

- `id` uses lowercase letters, digits, underscores, or hyphens and is at most 64 characters.
- A remote `base_url` is absolute HTTPS without user information, query, or fragment.
- The Codex-facing `protocol` is currently `responses`.
- `upstream_protocol` is `openai_responses` or `anthropic_messages`; the model name does not choose it.
- Retry counts are at most 10; idle timeout is between one second and one hour.
- Authentication uses `keychain`, `env`, or `env_header`, never an inline secret.

## Model

```json
{
  "id": "model-a",
  "provider": "example_provider",
  "remote_model": "vendor-model-name",
  "agent": "example_worker",
  "reasoning_effort": "high",
  "display_name": "Example Model",
  "context_window": 200000,
  "max_context_window": 200000,
  "effective_context_window_percent": 95,
  "supports_parallel_tool_calls": true,
  "supports_search_tool": false
}
```

Context values are user/provider declarations, not hard-coded vendor limits. Verify the real runtime with `codex debug models` and `session_audit.py`. When the worker cannot observe a value, report it as unavailable rather than substituting the catalog declaration.

## Anthropic Messages adapter

Declare the upstream protocol and optional local runtime settings:

```json
{
  "upstream_protocol": "anthropic_messages",
  "local_adapter": {
    "listen_host": "127.0.0.1",
    "listen_port": 18766,
    "max_output_tokens": 4096
  }
}
```

`local_adapter` does not select the protocol. If omitted, the installer uses loopback, a derived unique port, and a 16384 output limit. The generated Codex provider points to the local Responses service; the adapter calls the provider's real `base_url` with Anthropic Messages.

Schema v1 `adapter.type: anthropic_messages` remains readable for migration. New schema v2 manifests reject that old field so the protocol has one source of truth.

### Input conversion

- Responses instructions and system content become Anthropic `system`.
- User and assistant text become Anthropic message blocks.
- Visible native `agent_message` text is forwarded.
- `encrypted_content` is neither decrypted nor forwarded; ciphertext-only input is rejected.
- Function tools become Anthropic tools; freeform `apply_patch` becomes one string `input`.
- `multi_agent_v1`, `web_search`, `view_image`, and `request_user_input` are filtered.
- Reasoning effort maps to `output_config.effort`; `xhigh` and `ultra` map to upstream `max`.

### Output conversion

- Anthropic text becomes a Responses message.
- `tool_use` becomes `function_call` or `custom_tool_call` with the original `call_id`.
- Non-streaming and SSE streaming are supported; disconnect closes the upstream response.
- Whitespace-only output without a tool call becomes HTTP 502 `upstream_invalid_response`.
- The effective output limit is the minimum of the Codex request, adapter policy, and hard 64000 cap.

Retries can multiply the provider's requested output budget. For example, a 4096 limit with two transport retries may request up to 12288 output tokens across three upstream attempts, while each response remains capped at 4096.

## Fallback classification

Switch-eligible exhausted errors:

- `network`
- `timeout`
- `rate_limit`
- `billing`
- `service_unavailable`

Blocking errors:

- `auth`
- `invalid_request`
- `model_not_found`
- `task_failure`
- unknown errors

Fallback is not a catch-all retry. Switching on an authentication or model-name error would hide a broken configuration.

## Add another protocol adapter

Keep two boundaries:

1. `model_manifest.py` declares and strictly validates `upstream_protocol` without implementing the protocol.
2. A protocol module converts Responses requests/results; the HTTP service owns authentication forwarding, SSE, timeout, audit, and status mapping.

Add input/output conversion tests, tool-loop tests, interrupted-stream tests, empty-response tests, redirect credential tests, health identity tests, install/uninstall service tests, and a real Codex Desktop worker report.
