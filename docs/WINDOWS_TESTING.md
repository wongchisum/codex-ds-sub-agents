English · [简体中文](zh-CN/WINDOWS_TESTING.md)

# Windows real-machine compatibility test

The repository implements Windows Credential Manager, Task Scheduler service
management, cross-platform file locking, and dynamic Python interpreter paths.
Automated tests exercise Windows branches on macOS, but they do not replace a
real Windows 10 or 11 run.

## Record the environment

Save this output from PowerShell without saving credential values:

```powershell
$PSVersionTable.PSVersion
py -3 --version
py -3 -c "import platform,sys; print(platform.platform()); print(sys.executable)"
codex --version
git rev-parse HEAD
```

Also record the Codex Desktop version, Windows build, manifest name, and the
provider's `upstream_protocol`.

## Install and store credentials

```powershell
py -3 scripts\configure.py --list-model-protocols
py -3 scripts\configure.py --manifest config\my-team.local.json --name my-team
```

The first run should stop with exit code 3 and print an interactive
`credential_store.py set` command. Run that exact command, then repeat the same
configure command. Confirm:

- No key appears in the command line, PowerShell history, manifest, or configure
  log.
- `%USERPROFILE%\.codex\helpers\credential_store.py` is installed.
- The authentication command in `config.toml` uses the detected Python
  executable and helper, with no `/usr/bin/security` path.
- An Anthropic Messages provider creates a Task Scheduler task for the current
  user without requiring administrator privileges.
- Adapter `/health`, doctor, and Codex strict configuration checks pass.

## Run automated tests

```powershell
py -3 -m unittest discover -s tests -v
py -3 -m unittest tests.test_platform_runtime tests.test_platform_lock tests.test_adapter_service -v
py -3 -m unittest tests.test_credential_store tests.test_configure tests.test_diagnostics -v
```

Record pass, failure, and skip counts together with every original error. Do
not remove valid tests to obtain a green run.

## Run the Codex Desktop flow

Start a new Codex task after each agent installation or switch:

1. Read `subagent-selection.json` and verify the primary, agent, provider, and
   remote model.
2. Use `$codex-custom-subagents` for a read-only task and confirm the atomic
   claim receipt contains the expected agent, model, and provider.
3. Run a second tool loop that edits a temporary test file and executes tests;
   the parent must inspect the real artifact.
4. Complete the claim and confirm a `completed` receipt with exit code 0.
5. Restart Codex Desktop or sign in to Windows again, then confirm Task
   Scheduler restarts the adapter.
6. Create a controlled `timeout` or `service_unavailable` failure and confirm
   the whole batch switches to one fallback. Authentication failure must not
   switch.
7. Run uninstall with `--dry-run` first, then confirm uninstall removes only
   registered files whose digests still match, plus the owned scheduled task.

## Collect diagnostics

Keep the failed state intact and run:

```powershell
py -3 scripts\diagnostics.py --run windows_case_01 --out diagnostics --format zip --manifest "$env:USERPROFILE\.codex\custom-subagents\manifests\my-team.json"
```

The archive should contain Windows, Python, and Codex versions; redacted
manifest, selection, and install-registry summaries; adapter health and log
tails; the latest configure stage logs; and mailbox receipt/run-state summaries.

The tool does not collect `config.toml`, environment-variable values, Credential
Manager contents, prompts, or task bodies. Before uploading the archive, unpack
it and search for `sk-`, `Bearer`, `api_key`, and provider-specific key prefixes.
Do not upload it if plaintext credentials are present.

## Acceptance statement

Mark Windows as verified only after installation, restart, a real worker tool
loop, fallback, uninstall, and diagnostic inspection all pass. Before that,
state: "Windows implementation complete; real-machine verification pending."
