# Codex Custom Subagents

English · [简体中文](README.zh-CN.md)

[Documentation](docs/README.md) · [Installation](docs/INSTALLATION.md) · [Configuration](docs/CONFIGURATION.md) · [Troubleshooting](docs/TROUBLESHOOTING.md)

`codex-custom-subagents` helps Codex Desktop use custom model providers as subagents to complete tasks. It supports macOS and Windows, keeps credentials outside prompts and manifests, and lets Codex delegate work through the `$codex-custom-subagents` skill.

![Custom subagent tasks in Codex Desktop](assets/codex-custom-subagents.png)

## Ask Codex to install it

Open this repository in Codex Desktop and send the following prompt:

```text
Install and configure Codex Custom Subagents from this repository:
https://github.com/wongchisum/codex-custom-subagents

1. Detect the operating system, Python interpreter, Codex version, and Git.
   Use py -3 on Windows and python3 on macOS.
2. Read README.md, docs/INSTALLATION.md, docs/CONFIGURATION.md, and
   docs/MIGRATION.md before changing files.
3. Check for managed legacy skills named deepseek-delegation or
   codex-custom-agents. Preview migration before removing owned files, and
   preserve modified or unknown files.
4. Ask me to choose one primary model/protocol preset, then ask separately
   whether I want ordered fallbacks.
5. Collect only provider URLs, remote model names, protocol choices, context
   declarations, output limits, and credential reference names. Never ask me
   to paste an API key into Codex.
6. Run the explicit configure.py command for my choices. If it exits with code
   3, show the safe interactive credential command and wait for me to run it
   locally.
7. Re-run the same configure command after credentials are stored, then run
   doctor. Do not claim success unless doctor passes.
8. Tell me to close this Codex task and start a new task because an open task
   cannot hot-load newly installed agent types.
9. In the new task, use $codex-custom-subagents. Do not use either legacy skill
   name.
10. Do not print, copy, commit, or upload credential values.
```

The standalone prompt is also available in [docs/PROMPT_INSTALLATION.md](docs/PROMPT_INSTALLATION.md).

## Use it in Codex

After installation, start a **new Codex task** and use a prompt like this:

```text
Use $codex-custom-subagents to delegate the following work to the configured
custom subagent:

<describe the task>

Read subagent-selection.json, use its active.agent for the batch, verify the
worker's artifacts and tests, and report the accepted result.
```

For independent parallel work:

```text
Use $codex-custom-subagents to split this work into independent subagent tasks.
Use one active model for the whole batch, give each worker a bounded scope, and
verify every result before accepting it.
```

## What it actually does

This plugin installs provider-backed agent definitions into Codex Desktop. It
does not replace Codex or expose a generic chat UI. The parent Codex task keeps
control of planning and acceptance while custom subagents use Codex repository
tools and permissions in the same workspace.

```text
Your request
  → parent Codex selects the configured primary agent
  → bounded tasks are written to .codex-custom-subagents/pending/
  → custom subagents atomically claim different tasks
  → workers inspect or edit files and run commands/tests
  → parent Codex verifies artifacts and accepts the results
  → eligible transport failure can switch the whole batch to a fallback agent
```

Typical uses include:

- Split a repository review into architecture, security, test, and documentation tasks.
- Give a long code investigation or isolated implementation to a selected custom model.
- Run independent fixes in parallel while the parent Codex task reviews every diff and test result.
- Configure different providers for primary and ordered fallback without putting API keys in task files.

The workspace should ignore both the current mailbox and the legacy upgrade
path because task files can contain private repository context:

```gitignore
/.codex-custom-subagents/
# Keep this when upgrading from releases before the mailbox rename.
/.deepseek-delegations/
```

New batches never write to the legacy directory. If it contains unfinished
pre-upgrade work, finish that batch with explicit `--legacy-mailbox` mode; do
not rename or merge live mailboxes. See [migration](docs/MIGRATION.md).

## Supported platforms, protocols, and configuration

| Area | Verified support | What it means |
| --- | --- | --- |
| Codex Desktop hosts | macOS and Windows 10/11 | Cross-platform installer, credential access, service management, file locking, task claiming, recovery, and diagnostics. Linux is not currently claimed as a supported host. |
| Codex-facing API | Responses | Every generated provider speaks the Responses protocol expected by Codex. |
| OpenAI-style upstream | `openai_responses` | Codex calls an OpenAI Responses-compatible provider URL directly. This does not claim Chat Completions compatibility. |
| Anthropic upstream | `anthropic_messages` | A loopback adapter translates Codex Responses requests, tool calls, streaming events, responses, and usage to/from Anthropic Messages. |
| Custom configuration | JSON manifest schema v2 | Define provider URLs, remote model names, protocol, credential references, context/output declarations, agent names, one primary model, and ordered fallbacks. |

Here, cross-platform means the same plugin can be installed and run in Codex
Desktop on macOS and Windows. It does not mean live task synchronization
between devices.

Built-in presets are examples, not a closed provider list. A custom manifest can
point at another service that implements one of the two supported upstream
protocols. The manifest stores only credential references; values remain in
macOS Keychain, Windows Credential Manager, or the selected environment
variable.

Minimal shape:

```json
{
  "schema_version": 2,
  "providers": [
    {
      "id": "my_provider",
      "name": "My Provider",
      "base_url": "https://provider.example/api",
      "protocol": "responses",
      "upstream_protocol": "openai_responses",
      "auth": { "type": "env", "variable": "MY_PROVIDER_API_KEY" }
    }
  ],
  "models": [
    {
      "id": "my_model",
      "provider": "my_provider",
      "remote_model": "model-name",
      "agent": "my_model_worker",
      "context_window": 128000,
      "max_context_window": 128000,
      "effective_context_window_percent": 95
    }
  ],
  "selection": { "primary": "my_model", "fallbacks": [] }
}
```

See [configuration](docs/CONFIGURATION.md) for the complete schema and
[protocol adapters](docs/MODEL_ADAPTERS.md) for the exact conversion boundary.

## Other features

- One primary model per batch with ordered fallback switching for eligible transport failures.
- macOS Keychain, Windows Credential Manager, and environment-variable credential references.
- macOS LaunchAgent and Windows Task Scheduler service management.
- Atomic task claiming, durable receipts, recovery, redacted diagnostics, and safe uninstall ownership.
- Managed migration from `$deepseek-delegation` and `$codex-custom-agents` to `$codex-custom-subagents`.

## Manual quick start

Requirements: Codex Desktop signed in, Codex `0.146.0` or newer, Python 3.9+, Git, and credentials for the selected provider.

macOS:

```bash
git clone https://github.com/wongchisum/codex-custom-subagents.git
cd codex-custom-subagents
python3 scripts/configure.py --list-model-protocols
python3 scripts/configure.py --primary deepseek-anthropic
```

Windows PowerShell:

```powershell
git clone https://github.com/wongchisum/codex-custom-subagents.git
cd codex-custom-subagents
py -3 scripts\configure.py --list-model-protocols
py -3 scripts\configure.py --primary deepseek-anthropic
```

Available built-in model/protocol presets:

- `deepseek-anthropic`
- `deepseek-openai`
- `gemini-anthropic`
- `claude-anthropic`

Declare fallbacks separately from the primary model:

```bash
python3 scripts/configure.py \
  --primary claude-anthropic \
  --fallback gemini-anthropic \
  --fallback deepseek-openai
```

If `configure.py` exits with code 3, run the printed credential command locally and repeat the same configure command. After installation or migration, create a new Codex task before testing delegation; otherwise the old task can return `unknown agent_type`.

## Documentation

- [Documentation index](docs/README.md)
- [Installation, upgrade, and uninstall](docs/INSTALLATION.md)
- [Model and fallback configuration](docs/CONFIGURATION.md)
- [Providers and protocol adapters](docs/MODEL_ADAPTERS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Migration](docs/MIGRATION.md)
- [Testing](docs/TESTING.md)
- [Windows real-machine checklist](docs/WINDOWS_TESTING.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)

`doctor.py` validates local installation state, credential references, strict Codex configuration, and adapter health. It does not call a real model or validate billing. CI runs on both macOS and Windows; the maintainer also reported the complete Windows PR #1 real-machine checklist passing on 2026-08-03. Later releases still require a new checklist run.

This is a third-party integration, not an official OpenAI, Anthropic, Google, or DeepSeek product.

## Links

[Linux Do](https://linux.do/)
