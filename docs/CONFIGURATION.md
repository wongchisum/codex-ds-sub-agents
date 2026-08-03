# Model and fallback configuration

English · [简体中文](zh-CN/CONFIGURATION.md)

The same manifest and selection format is used on macOS and Windows. Replace
`python3` with `py -3` when running these commands in Windows PowerShell.

## Built-in model/protocol presets

```bash
python3 scripts/configure.py --list-model-protocols
```

| CLI name | Display name | Upstream protocol |
| --- | --- | --- |
| `deepseek-anthropic` | deepseek (anthropic) | `anthropic_messages` |
| `deepseek-openai` | deepseek (openai) | `openai_responses` |
| `gemini-anthropic` | gemini (anthropic) | `anthropic_messages` |
| `claude-anthropic` | claude (anthropic) | `anthropic_messages` |

Preset names are conveniences. `upstream_protocol` defines the actual provider protocol; a model name never selects a protocol.

## Primary and fallbacks

Primary only:

```bash
python3 scripts/configure.py --primary deepseek-anthropic
```

Ordered fallbacks:

```bash
python3 scripts/configure.py \
  --primary claude-anthropic \
  --fallback gemini-anthropic \
  --fallback deepseek-openai
```

The primary and fallbacks must be unique. `max_switches` is derived from the number of fallbacks. Every running worker batch uses one active agent; after an eligible exhausted failure, the parent starts a replacement batch with the next fallback.

## Custom manifest

Copy a `config/*.example.json` file to the ignored `config/*.local.json` pattern and configure:

- Provider: `base_url`, `upstream_protocol`, credential reference, retry policy, and local adapter port.
- Model: `remote_model`, `agent`, context declarations, output limit, and tool capabilities.
- Selection: one `primary`, ordered `fallbacks`, and `max_switches`.

```bash
python3 scripts/configure.py \
  --manifest config/my-team.local.json \
  --name my-team
```

The manifest may contain credential references, never credential values. Supported references are `keychain`, `env`, and `env_header`. Anthropic Messages providers are converted to the Responses protocol through a local adapter; each adapter needs a unique listen port.

“Custom” means provider URLs, remote model names, agent IDs, limits, credentials,
and selection are not hard-coded. It does not mean arbitrary wire protocols can
be loaded at runtime: `upstream_protocol` must currently be
`openai_responses` or `anthropic_messages`.

## Non-interactive use

Codex and CI should always pass one of `--primary`, `--manifest`, or the compatibility-only `--profile` option. Without an explicit source, `configure.py` refuses to guess when stdin is not interactive.

`--profile` remains for old scripts. New configurations declare the primary and fallbacks separately instead of encoding fallback choices in a profile name.
