# Codex Custom Subagents

[中文](README.md) · [Installation](docs/INSTALLATION.md) · [Architecture](docs/ARCHITECTURE.md) · [Model adapters](docs/MODEL_ADAPTERS.md)

`codex-custom-subagents` lets Codex Desktop create subagents backed by custom providers, model names, URLs, and protocols. A manifest may declare multiple candidates, but one batch uses exactly one model. After an eligible transport failure, the parent can switch the whole batch to the next configured fallback.

The repository is a Codex plugin containing the compatibility-named `deepseek-delegation` skill, an installer, model-catalog generation, an Anthropic Messages adapter, and macOS LaunchAgent / Windows Task Scheduler service management.

![Custom subagent tasks in Codex Desktop](assets/codex-custom-subagents.png)

The screenshot is from a local DeepSeek worker test. Claude, Gemini, DeepSeek's Anthropic endpoint, and other compatible Anthropic Messages gateways can also be configured.

## Quick start

Requirements: macOS, Codex Desktop signed in, Python 3.9+, `git`, and credentials for the selected provider.

```bash
git clone https://github.com/wongchisum/codex-custom-subagents.git
cd codex-custom-subagents
python3 scripts/configure.py --list-protocols
python3 scripts/configure.py --profile deepseek-anthropic
```

`configure.py` copies the selected manifest to
`~/.codex/custom-subagents/manifests/`, checks credential references, and only then
installs and runs doctor. When a credential is missing it stops before installation
with status 3 and prints a safe local command such as:

```bash
/usr/bin/security add-generic-password -U -a codex -s deepseek-api-key -w
```

With no value after `-w`, macOS reads the secret interactively. Save it locally and
run the same configure command again. Start a new Codex task after installation
because open tasks do not hot-load newly installed agent types.

Profiles are compatibility examples. New manifests use `schema_version: 2` and declare each Provider as `openai_responses` or `anthropic_messages`; model names do not select protocols.

Other compatibility profiles:

```bash
# Claude primary with Gemini fallback
python3 scripts/configure.py --profile claude-gemini

# Gemini only
python3 scripts/configure.py --profile gemini-anthropic

# Legacy fixed DeepSeek profile
python3 scripts/configure.py --profile legacy-deepseek

# Custom manifest generated from your provider details
python3 scripts/configure.py \
  --manifest config/my-team.local.json \
  --name my-team
```

## Ask Codex to install it

Send Codex the installation prompt in the Chinese README's
“让 Codex 帮你安装和配置” section, or ask it to follow these rules:

1. Inspect the repository and read `scripts/configure.py` before changing anything.
2. Confirm the primary model, ordered fallbacks, URLs, remote model names, protocol,
   Keychain service names, context declarations, and output limits.
3. Never request or store API-key values. Custom manifests contain references only.
4. Run one explicit `configure.py --profile ...` command, or generate an ignored
   `config/*.local.json` file and run `configure.py --manifest ... --name ...`.
5. On exit 3, show the printed Keychain command and stop for local user input.
6. Re-run the same configure command after confirmation. Report completion only
   when doctor passes, then ask the user to start a new Codex task.

## Usage

Ignore the durable mailbox in the target repository:

```gitignore
/.deepseek-delegations/
```

Then prompt Codex:

```text
Use $deepseek-delegation. Split review and test analysis into two independent
tasks. Resolve the active agent from subagent-selection.json and create both
workers with fork_turns: "none". Do not mix primary and fallback models in one batch.
```

`deepseek-delegation` remains the skill ID for backward compatibility; the selected manifest agent may use Claude, Gemini, DeepSeek, or another supported model.

## Documentation

- [Installation, upgrade, and uninstall](docs/INSTALLATION.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Providers and protocol adapters](docs/MODEL_ADAPTERS.md)
- [Windows real-machine testing and diagnostics](docs/WINDOWS_TESTING.md)
- [Implementation details](docs/IMPLEMENTATION.md)
- [Migration and compatibility](docs/MIGRATION.md)
- [Testing](docs/TESTING.md)
- [Current test report](TEST_REPORT.md)

## Verified boundaries

- The catalog minimum is `0.146.0`; the current complete test snapshot used `0.146.0-alpha.9.2`.
- A catalog can declare a 1M context window, while one native Desktop run reported an actual `model_context_window` of `258400`.
- `doctor.py` validates installation state, credential references, and adapter health. It does not make a real model request or validate billing.
- The tested Codex runtime accepts `responses` providers. Anthropic Messages is supported through the local adapter.
- Windows platform branches have automated simulated coverage but still require the documented Windows 10/11 real-machine acceptance run.

This is a third-party integration, not an official OpenAI, Anthropic, Google, or DeepSeek product.
