# Troubleshooting

English · [简体中文](zh-CN/TROUBLESHOOTING.md)

## Windows reports `Invalid argument/option - 'Create'`

Old builds passed unprefixed action names to `schtasks`. Supported calls use `/Query`, `/Create`, `/Run`, `/End`, and `/Delete`.

After updating to the Issue #2 fix, preview cleanup of the failed pending installation before removing it:

```powershell
py -3 scripts\uninstall.py --manifest "$env:USERPROFILE\.codex\custom-subagents\manifests\<name>.json" --no-stop-adapters --dry-run
```

Then rerun without `--dry-run` if the preview is correct. Do not delete the entire `%USERPROFILE%\.codex` directory.

## Installation succeeds but delegation returns `unknown agent_type`

Codex loads the agent registry when a task starts. Doctor cannot hot-load a newly installed agent into an already open task.

1. Finish configure and doctor.
2. Close the task that performed the installation.
3. Start a new Codex task.
4. Read `subagent-selection.json` and use `$codex-custom-subagent`.

Do not classify `unknown agent_type` as a network, billing, or rate-limit failure, and do not switch to a fallback.

## Worker startup fails with `401 missing bearer credential`

The loopback adapter requires Codex to provide the bearer token through the
configured provider auth command. Current installations run an auth preflight
before spawning a worker. If `delegation_runtime.py begin` returns
`auth_unavailable`, the worker was not started; reinject the credential and run
`begin` again under the same Windows user/security context as Codex. For a
Credential Manager entry, verify the exact service and account without printing
the value:

```powershell
& "<python.exe>" "$env:USERPROFILE\.codex\helpers\credential_store.py" exists `
  --account codex --service deepseek-api-key
```

Do not remove the adapter's bearer check or add the secret to a task file,
selection JSON, command-line argument, or diagnostic bundle. `401` after a
successful preflight indicates stale Codex provider configuration or a process
running under a different Windows security context; restart Codex after
reinstalling the generated provider configuration and compare `whoami` in the
same context.

## Windows resolves `python3` to the Store shim

Use `py -3` on Windows. To list the interpreters known to the Python Launcher:

```powershell
py -0p
```

If the launcher is unavailable, use `Get-Command python -All` to inspect candidates,
then run the explicit non-Store `python.exe` path. Generated agents, credential
helpers, and Task Scheduler commands use the interpreter that runs the installer
rather than a hard-coded `python3` command.

## A failed install leaves a pending installation

The ownership registry keeps a pending record so a retry cannot hide a partial transaction. Use the original manifest with `uninstall.py` to clean up registered resources. Modified files remain preserved.

## Create a redacted diagnostic bundle

```powershell
py -3 scripts\diagnostics.py --run windows_failure_01 --out diagnostics --format zip --manifest <manifest>
```

The bundle excludes Credential Manager values, environment values, `config.toml`, prompts, and task bodies. Inspect it for `sk-`, `Bearer`, `api_key`, and provider-specific key prefixes before sharing it.
