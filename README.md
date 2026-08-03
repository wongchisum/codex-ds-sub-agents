# Codex Custom Subagents

English · [简体中文](README.zh-CN.md)

[Documentation](docs/README.md) · [Installation](docs/INSTALLATION.md) · [Configuration](docs/CONFIGURATION.md) · [Troubleshooting](docs/TROUBLESHOOTING.md)

`codex-custom-subagents` helps Codex Desktop use custom model providers as subagents to complete tasks. It supports macOS and Windows, keeps credentials outside prompts and manifests, and lets Codex delegate work through the `$codex-custom-subagent` skill.

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
9. In the new task, use $codex-custom-subagent. Do not use either legacy skill
   name.
10. Do not print, copy, commit, or upload credential values.
```

The standalone prompt is also available in [docs/PROMPT_INSTALLATION.md](docs/PROMPT_INSTALLATION.md).

## Use it in Codex

After installation, start a **new Codex task** and use a prompt like this:

```text
Use $codex-custom-subagent to delegate the following work to the configured
custom subagent:

<describe the task>

Read subagent-selection.json, use its active.agent for the batch, verify the
worker's artifacts and tests, and report the accepted result.
```

For independent parallel work:

```text
Use $codex-custom-subagent to split this work into independent subagent tasks.
Use one active model for the whole batch, give each worker a bounded scope, and
verify every result before accepting it.
```

The target repository should ignore the durable task mailbox:

```gitignore
/.deepseek-delegations/
```

## Features

- Custom provider URLs, remote model names, context declarations, and output limits.
- Direct OpenAI Responses providers and Anthropic Messages providers through a local adapter.
- One primary model per batch with ordered fallback switching for eligible transport failures.
- macOS Keychain, Windows Credential Manager, and environment-variable credential references.
- macOS LaunchAgent and Windows Task Scheduler service management.
- Atomic task claiming, durable receipts, recovery, redacted diagnostics, and safe uninstall ownership.
- Managed migration from `$deepseek-delegation`, `$codex-custom-agents`, and `$codex-custom-subagents` to `$codex-custom-subagent`.

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

`doctor.py` validates local installation state, credential references, strict Codex configuration, and adapter health. It does not call a real model or validate billing. Windows branches have automated coverage, but release claims still require the documented Windows 10/11 real-machine run.

This is a third-party integration, not an official OpenAI, Anthropic, Google, or DeepSeek product.
