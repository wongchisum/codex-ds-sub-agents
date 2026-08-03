# Installation, upgrade, and uninstall

English · [简体中文](zh-CN/INSTALLATION.md)

## Recommended: let Codex install it

Open the repository in Codex Desktop and use the copyable [installation prompt](PROMPT_INSTALLATION.md). The prompt makes Codex detect the platform, ask for model choices without requesting secrets, run `configure.py`, and verify the result with doctor.

Manual steps follow below.

## Requirements

- macOS or Windows 10/11.
- Codex Desktop installed and signed in; Codex `0.146.0` or newer.
- Python 3.9+ and Git.
- A credential for the selected provider, stored outside the repository.

Windows release claims still require the [real-machine checklist](WINDOWS_TESTING.md); simulated platform tests do not replace it.

Clone the repository:

```bash
git clone https://github.com/wongchisum/codex-custom-subagents.git
cd codex-custom-subagents
```

## What gets installed

The installer writes runtime files under `CODEX_HOME`, which defaults to `~/.codex`:

```text
~/.codex/
├── agents/                              # worker definitions
├── models/                              # catalogs and subagent-selection.json
├── skills/codex-custom-subagent/        # $codex-custom-subagent
├── adapters/                            # Anthropic Messages adapter
├── logs/adapters/                       # adapter logs and structural audit
├── logs/custom-subagents/               # redacted configure logs
├── helpers/credential_store.py          # macOS/Windows credential boundary
├── custom-subagents/manifests/          # stable managed manifest copies
├── config.toml                          # model_providers blocks
└── .codex-subagent-installations.json   # ownership registry
```

Adapter manifests create a per-user LaunchAgent on macOS or Task Scheduler task on Windows. The adapter listens only on loopback and starts at login.

## Configure a built-in preset

List model/protocol presets:

```bash
python3 scripts/configure.py --list-model-protocols
```

Install one primary model:

```bash
python3 scripts/configure.py --primary deepseek-anthropic
```

Install a primary with ordered fallbacks:

```bash
python3 scripts/configure.py \
  --primary claude-anthropic \
  --fallback gemini-anthropic \
  --fallback deepseek-openai
```

On Windows, replace `python3` with `py -3` and use backslashes when convenient.

Built-in presets:

| Preset | Model | Upstream protocol |
| --- | --- | --- |
| `deepseek-anthropic` | DeepSeek V4 Flash | Anthropic Messages |
| `deepseek-openai` | DeepSeek V4 Flash | OpenAI Responses |
| `gemini-anthropic` | Gemini 3.5 Flash | Anthropic Messages |
| `claude-anthropic` | Claude Opus 4.6 | Anthropic Messages |

`configure.py`:

1. Strictly validates the selected manifest.
2. Saves a stable copy to `~/.codex/custom-subagents/manifests/<name>.json` with mode 0600 on POSIX.
3. Checks macOS Keychain, Windows Credential Manager, or environment credential references.
4. Stops with exit code 3 before installation when a required credential is missing.
5. Installs providers, agents, catalogs, the Skill, and adapter services after credentials exist.
6. Runs doctor automatically unless `--skip-doctor` is explicitly supplied.

The workflow is idempotent. After storing a missing credential, repeat the exact same configure command.

## Configure a custom manifest

Create an ignored `config/*.local.json` file from an example, then run:

```bash
python3 scripts/configure.py \
  --manifest config/my-team.local.json \
  --name my-team
```

`--name` controls the stable manifest filename and installation identity. If a stable file with that name already contains different data, configure refuses to overwrite it; choose another name or uninstall the existing configuration first.

See [configuration](CONFIGURATION.md) and [provider adapters](MODEL_ADAPTERS.md) for the manifest schema.

## Store credentials safely

Manifests may contain references to secrets, never inline values such as `api_key`, `key`, `secret`, `token`, or `value`.

macOS Keychain:

```bash
/usr/bin/security add-generic-password -U -a codex -s deepseek-api-key -w
```

Leaving the value absent after `-w` makes Keychain prompt interactively, so the key does not enter shell history.

Windows Credential Manager:

```powershell
py -3 scripts\credential_store.py set --account codex --service deepseek-api-key
```

The helper prompts for the value. Do not append it to the command.

Environment references are also supported:

- `auth.type = "env"`: use an environment variable as a bearer token.
- `auth.type = "env_header"`: place an environment variable in a named authentication header.

Persistent services may not inherit an interactive shell environment. Prefer Keychain or Credential Manager for local long-running adapters.

## Verify the installation

```bash
python3 scripts/doctor.py \
  --manifest ~/.codex/custom-subagents/manifests/deepseek-anthropic.json
```

Doctor checks selection, providers, agents, catalogs, credential references, strict Codex configuration, and adapter `/health` identity/fingerprint. It does not call a real model or validate network reachability, billing, model permission, or a complete tool loop.

After configure and doctor pass, close the current Codex task and start a new task. Open tasks do not hot-load agent definitions and may return `unknown agent_type`.

## Export redacted diagnostics

```bash
python3 scripts/diagnostics.py \
  --run windows_case_01 \
  --out diagnostics \
  --format zip \
  --manifest ~/.codex/custom-subagents/manifests/my-team.json
```

The bundle contains bounded platform, manifest, selection, ownership, adapter-health, log-tail, receipt, and run-state evidence. It excludes credential values, environment values, `config.toml`, prompts, and task bodies. Inspect the archive before sharing it.

## Upgrade and ownership rules

- Writes use a same-directory temporary file followed by atomic replacement.
- Changed managed files receive a UTC `.bak.*` backup before replacement.
- A provider with the same ID but different URL, authentication, protocol, or adapter settings causes installation to stop.
- `.codex-subagent-installations.json` tracks digests, owners, order, and selection so shared files survive removal of one installation.
- The legacy `.codex-deepseek-manifest.json` filename remains for old ownership detection.
- Managed manifests under `~/.codex/custom-subagents/manifests/` keep installation identity stable when the source checkout moves.

Use [migration](MIGRATION.md) when upgrading from `$deepseek-delegation` or `$codex-custom-agents`.

## Uninstall

Preview first:

```bash
python3 scripts/uninstall.py \
  --manifest ~/.codex/custom-subagents/manifests/deepseek-anthropic.json \
  --dry-run
```

Then uninstall:

```bash
python3 scripts/uninstall.py \
  --manifest ~/.codex/custom-subagents/manifests/deepseek-anthropic.json
```

The uninstaller removes only files whose digest still matches and that have no remaining owner. It preserves modified files, symlinks, unknown files, and resources shared by another installation. Removing the active selection restores the previous valid registered selection when one exists.

If the original manifest file is missing, its recorded path can be used with `--no-stop-adapters` to clean owned files without attempting to identify and stop adapter services.
