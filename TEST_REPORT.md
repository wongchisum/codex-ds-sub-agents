# Codex Custom Subagents test report

Date: 2026-08-02
Environment: macOS, Python 3.9.6 and 3.13, Codex CLI 0.146.0-alpha.9.2

No API credential is stored in this repository, generated config, task pool, or
test output.

## Implemented scope

- Declarative JSON manifest with custom provider URL, remote model name,
  protocol, and Keychain/environment credential reference.
- Schema v2 Provider abstraction uses `openai_responses` or
  `anthropic_messages`; schema v1 adapter declarations remain readable.
- One primary model, ordered unique fallbacks, maximum switch count, and
  deterministic generation.
- Fallback only for network, timeout, rate-limit, billing, and service
  unavailable failures.
- Custom manifest installation renders two internal agents and two provider
  blocks while exposing one active primary selection.
- Doctor checks every rendered provider/agent and runs Codex strict config.
- Custom uninstall removes unchanged managed artifacts in reverse provider
  order.
- Loopback Anthropic Messages adapter converts Responses messages, native Desktop
  `agent_message`, function/custom tools, tool outputs, usage, errors, and terminal
  SSE events to/from Anthropic Messages. It forwards visible task text and drops
  `encrypted_content`.
- Model catalogs are generated from each manifest, including model-specific
  context limits and supported local tools.
- A unified `configure.py` entrypoint selects built-in profiles or a custom
  manifest, stores a stable 0600 copy, checks credential references before
  installation, and runs doctor when credentials are available.
- Cross-platform runtime paths include macOS LaunchAgent/Keychain and Windows
  Task Scheduler/Credential Manager. Windows branches have simulated tests only.
- Configure phases, adapter audit/output tails, mailbox receipts, and runtime
  state can be exported in a bounded redacted diagnostic bundle.
- Skill packaging ignores interpreter/OS caches and copies valid binary assets
  without decoding them as text, so repeated installs are Python-version safe.

## Automated results

| Area | Result | Evidence |
|---|---:|---|
| Selection policy | PASS | 24 focused unit tests |
| Adapter protocol | PASS | 17 focused unit tests |
| Manifest validation/rendering | PASS | verified Gemini route, adapter, catalog, and loopback validation covered |
| Custom install and doctor | PASS | isolated Codex home, strict config loaded |
| Windows platform simulation | PASS | win32 import, Task Scheduler XML/lifecycle mocks, `msvcrt` lock dispatch, dynamic Python/path rendering |
| Credential and diagnostics | PASS | native-store boundary, configure JSONL redaction, bounded diagnostic zip with task/config exclusions |
| Full automated suite | PASS | 301 passed on Python 3.13; Python 3.9 passed 298 and skipped 3 TOML checks |
| Python compilation | PASS | bytecode cache redirected to `/private/tmp` |

The plugin manifest and `$codex-custom-subagents` Skill were also validated with
their Codex validators after the project rename. This report retains older
endpoint and session evidence below; historical local paths are evidence, not
current product names.

## Endpoint discovery

Targets:

- `claude-opus-4-6` at `https://api.aicodemirror.ai/api/claudecode`
- `gemini 3.5-flash` at `https://api.aicodemirror.ai/api/gemini`

Unauthenticated probes reached both hosts. Base URLs returned HTTP 404; candidate
API paths including `/responses` reached the authentication layer and returned
HTTP 401 JSON. This proves reachability and path handling only. It does not prove
that either endpoint implements the complete Responses API required by Codex.

Runtime configuration probes produced:

- `wire_api = "messages"`: rejected; expected `responses`.
- `wire_api = "chat"`: rejected as no longer supported.
- `wire_api = "responses"`: strict configuration accepted.

## Credentialed live results

The credential was read from macOS Keychain through the configured command
reference. It was not copied into a command argument, generated file, log, or
test report.

| Provider | Result | Service evidence | Codex evidence |
|---|---:|---|---|
| Claude direct Responses / `opus 4.6` | FAIL | `/responses` returned the gateway HTML frontend | Sampling failed before `response.completed` |
| Claude adapter / `claude-opus-4-6` | PASS | `/v1/messages` returned canonical Claude content and tool calls | Text, `exec_command`, tool output, and `apply_patch` loops completed |
| Gemini / `gemini 3.5-flash` | FAIL | `SETTLEMENT_UNKNOWN_MODEL`: the model cannot be mapped to a billing key | The sampling request returned the same settlement error and the turn failed |
| ClaudeCode.net.cn Gemini / `gemini-3.5-flash` | PASS | `/v1/messages` returned canonical Anthropic JSON and exact text marker | Adapter completed a real `pwd` tool loop and returned the exact tool output |
| DeepSeek Anthropic / `deepseek-v4-flash` | PASS | `https://api.deepseek.com/anthropic/v1/messages` returned text and `tool_use` | Exact text marker and `exec_command` → tool result → final response completed |

### DeepSeek Anthropic 1M catalog test

- Manifest: `config/deepseek-anthropic-1m.example.json`.
- `codex debug models` reported `context_window = 1048576`,
  `max_context_window = 1048576`, and `effective_context_window_percent = 95`.
- Isolated install, doctor, Keychain lookup, strict config, adapter health, plain text,
  and the `exec_command` tool loop passed.
- A native Desktop sub-agent rollout later reported `model_context_window = 258400`
  in its `turn_context`, despite the 1M catalog declaration. This validates catalog
  generation and normal execution, not the effective native sub-agent limit. No
  high-cost boundary load was sent.

### Native Desktop `agent_message` compatibility

The first native `deepseek_anthropic_worker` attempt selected the intended provider
and model, but failed before claiming the mailbox task with:

```text
invalid_request_error: unsupported Responses input item: agent_message
```

The recorded payload contains a visible `input_text` task envelope and a separate
`encrypted_content` block. The adapter now maps the visible text to an Anthropic
user message and never forwards the ciphertext. Unit tests cover `NEW_TASK`, string
follow-up, unknown blocks, and ciphertext-only rejection.

The fixed adapter was installed into the active Codex home and doctor passed all
checks. The already-open parent task could not spawn the newly installed custom
agent because its agent registry was cached and returned `unknown agent_type`.
That failure happened locally before any provider request. Native retest therefore
required a new Desktop task.

The new-task retest passed end to end in parent thread
`019fbe18-34c2-7182-a27e-6366400e754b` and worker thread
`019fbe18-97b2-74b0-a308-88ea3765a5b0`:

- Desktop loaded `deepseek_anthropic_worker` with model `deepseek-v4-flash` and
  provider `deepseek_anthropic`.
- The adapter returned HTTP 200 for all 14 recorded `/responses` requests.
- The worker atomically claimed `compat_manual_test` as
  `56588-8f16c50aad7d`, and the claimed file and receipt exist.
- The worker, not the parent, ran
  `python3 -m unittest tests.test_model_manifest -v`; 13 tests passed with exit 0.
- The native rollout reported `model_context_window = 258400`, so the 1M catalog
  declaration is still not the effective Desktop worker limit.

Both original AI Code Mirror hosts are reachable and the Keychain credential is
available to Codex. Claude completes Codex Responses requests through the local
adapter; the original AI Code Mirror Gemini route still fails before a baseline
response.

The original Gemini result above applies to
`https://api.aicodemirror.ai/api/gemini` with display-style model name
`gemini 3.5-flash`. A separate 2026-08-02 test of
`https://api.claudecode.net.cn/api/gemini` with exact model identifier
`gemini-3.5-flash` passed. The new endpoint accepts Anthropic Messages at
`/v1/messages`; it is not a direct Codex Responses endpoint for this integration.

### ClaudeCode.net.cn Gemini adapter results

- An authenticated minimal Anthropic request returned HTTP 200, canonical
  `type: message`, model `gemini-3.5-flash`, and exact marker
  `GEMINI_BASELINE_OK`.
- An isolated generated provider, agent, and model catalog passed doctor,
  Keychain lookup, adapter health, wire API, and Codex strict-config checks.
- A real Codex turn called `/bin/zsh -lc pwd`; the command exited 0 and Gemini
  returned `GEMINI_TOOL_OK /Users/a1234/Documents/codex-deepseek-subagents`.
- The test credential was read from macOS Keychain and was not written to the
  manifest, command arguments, report, or model output.
- A CLI-only native spawn attempt was inconclusive because that isolated main
  task did not expose `spawn_agent`; Gemini executed inspection commands itself.
  The mailbox task remained pending and the attempt is not counted as a native
  sub-agent pass or failure.
- The dedicated Gemini manifest was installed into the active Codex home. It
  added `claudecode_gemini_worker`, the generated Gemini catalog, and the
  `claudecode_gemini` provider while preserving existing DeepSeek files. The
  active `subagent-selection.json` now selects Gemini, the loopback adapter is
  listening on port 18768, and active-home doctor passed every check.

### Gemini native Desktop result and output guard

Native parent thread `019fbe30-da73-73b0-b0e9-c19b779a8e57` spawned worker
`019fbe31-8362-7832-800b-a7c79c688154`. The worker claimed the mailbox task,
read the requested source, and ran 14 tests successfully. The first worker turn
then completed with an empty final message after about 240 seconds; its final
gateway usage entry reported 62,928 output tokens. A follow-up to the same worker
returned the report in about four seconds.

The adapter now caps Gemini upstream output at 4,096 tokens and rejects
whitespace-only responses without tool calls as HTTP 502
`upstream_invalid_response`. With two configured transport retries, the maximum
output budget across three failed attempts is 12,288 tokens. Worker instructions
also distinguish declared catalog context from runtime observation; the native
rollout reported `model_context_window = 258400`, not the declared 1M value.
After installing the guard, a credentialed Codex `pwd` tool loop completed with
the exact expected marker and 375 reported output tokens, confirming that the
4,096 cap does not break normal text and tool execution.

### Claude protocol isolation

Additional tests separated service availability from Codex compatibility:

- `POST /api/claudecode/responses` returned HTTP 200 with the gateway's HTML
  frontend instead of a Responses JSON/SSE stream. This directly explains why
  Codex never received `response.completed`.
- A minimal Anthropic request to `/api/claudecode/v1/messages` reached the
  Claude route but returned HTTP 503 with a Claude Code request-shape error.
- Official Claude Code 2.1.220, configured with the same base URL and Keychain
  credential, successfully called canonical model `claude-opus-4-6` and returned
  the expected marker in one turn.
- Passing the display string `opus 4.6` directly to Claude Code produced no API
  usage or tokens for 100 seconds and was aborted. It is not a usable API model
  identifier in this integration.

Therefore the Claude service and credential are healthy. The unsupported edge
is Codex Responses transport to a Claude Code/Anthropic-compatible endpoint.

### Adapter end-to-end results

- Plain text returned the expected marker and a valid `response.completed`.
- Claude called `exec_command`; Codex executed `pwd`, returned the matching
  `call_id`, and Claude produced the final response.
- Claude called freeform `apply_patch`. Two invalid patch attempts returned tool
  errors; Claude corrected the syntax and changed an isolated file from
  `before` to `after` on the third attempt.
- The adapter installed under an isolated `CODEX_HOME/adapters` was started from
  the exact command printed by the installer and passed another tool loop.
- A credentialed probe confirmed `output_config.effort = high` is accepted by
  the upstream. The adapter forwards effort without enabling stateful thinking.

## Readiness decision

Claude and DeepSeek Anthropic adapter baselines and local tool execution are
**ready**. The native Desktop payload incompatibility is fixed, installed, and
validated through a real mailbox claim, local tool calls, test execution, and final
worker response. Newly installed agent types still require a new Desktop task when
the current task has already cached its agent registry.

Legacy `deepseek_worker` and manifest workers now use isolated catalog paths.
Tests cover uninstalling the custom version first and uninstalling the legacy
version first. In both orders, the remaining agent, catalog, provider,
selection, skill, and adapter stay usable.

The compatibility build was then installed beside the existing legacy agent in
the active `~/.codex`. Doctor passed provider, Keychain, catalog, selection,
adapter, agent, wire API, and strict-config checks. The active loopback health
endpoint returned `anthropic_messages` with status `ok`.

End-to-end Claude → Gemini fallback has not been verified. The new
ClaudeCode.net.cn Gemini route has completed its independent text and tool-loop
baseline. `config/model-providers.example.json` now uses the verified endpoint,
exact model identifier, and Anthropic Messages adapter. A dedicated
`config/gemini-anthropic.example.json` activates only Gemini for native testing.
Do not claim fallback readiness until the new Gemini agent completes a native
Desktop spawn and a categorized primary failure triggers the switch.

Required before rerunning:

1. Install `config/gemini-anthropic.example.json`, start its adapter, and open a
   new Desktop task to complete a native Gemini mailbox claim.
2. Reinstall `config/model-providers.example.json` for the fallback profile.
3. Inject an eligible primary failure such as timeout, HTTP 429, billing
   exhaustion, or HTTP 503 and verify the single ordered switch to Gemini.

## Still not verified

- Windows 10/11 real-machine installation, `schtasks` lifecycle, Credential
  Manager access, Desktop worker tool loop, fallback, restart, and uninstall.

- Effective 1M context in a native Desktop sub-agent; the observed runtime value is
  `258400` even though `codex debug models` reads the 1M catalog declaration.
- Native Desktop `spawn_agent` for `gemini-3.5-flash` through the new
  ClaudeCode.net.cn Anthropic Messages endpoint.
- Real simultaneous parallel tool calls and compaction.
- Incremental upstream token streaming; the adapter currently buffers one
  complete Anthropic response before emitting Responses events.
- Real billing/rate-limit error classification from the service.
- Automatic observation of provider errors from native worker final responses.
- End-to-end fallback after a live primary failure.
