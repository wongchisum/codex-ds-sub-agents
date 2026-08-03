# Documentation

English · [简体中文](zh-CN/README.md)

`codex-custom-subagents` helps Codex Desktop use custom model providers as subagents to complete tasks on macOS and Windows.

## Start with Codex

1. Copy the [installation prompt](PROMPT_INSTALLATION.md) into Codex Desktop.
2. Let Codex inspect the platform, collect non-secret provider settings, run the installer, and verify the result.
3. Start a new Codex task after installation.
4. Use `$codex-custom-subagents` to delegate work.

Example usage:

```text
Use $codex-custom-subagents to delegate this task to the configured custom
subagent. Read subagent-selection.json, use its active.agent, and verify the
worker's artifacts and tests before accepting the result.
```

## What the plugin enables

- Delegate repository analysis, bounded implementation, tests, and review to
  provider-backed Codex subagents while the parent task remains responsible for
  acceptance.
- Run the same installation and mailbox workflow on macOS and Windows 10/11.
- Connect directly to OpenAI Responses-compatible upstreams or translate
  Anthropic Messages through the included loopback adapter.
- Define custom providers, models, credential references, context declarations,
  primary selection, and ordered fallbacks in a validated JSON manifest.

The built-in DeepSeek, Claude, and Gemini presets demonstrate the configuration;
they are not the only usable services. The actual requirement is one of the two
implemented upstream protocols. See [provider adapters](MODEL_ADAPTERS.md) for
the precise boundary and [configuration](CONFIGURATION.md) for the manifest.

## Install and configure

- [Installation, upgrade, and uninstall](INSTALLATION.md)
- [Model and fallback configuration](CONFIGURATION.md)
- [Providers and protocol adapters](MODEL_ADAPTERS.md)
- [Prompt: ask Codex to install it](PROMPT_INSTALLATION.md)

## Architecture and maintenance

- [Architecture](ARCHITECTURE.md)
- [Implementation](IMPLEMENTATION.md)
- [Skill migration](MIGRATION.md)
- [Troubleshooting](TROUBLESHOOTING.md)

## Test and release

- [Automated and Desktop testing](TESTING.md)
- [Windows real-machine checklist](WINDOWS_TESTING.md)
- [Current test report](../TEST_REPORT.md)

Suggested reading paths:

- First install: `PROMPT_INSTALLATION` → `INSTALLATION` → `CONFIGURATION`
- Upgrade: `MIGRATION` → `INSTALLATION` → `TROUBLESHOOTING`
- New protocol: `ARCHITECTURE` → `MODEL_ADAPTERS` → `IMPLEMENTATION`
