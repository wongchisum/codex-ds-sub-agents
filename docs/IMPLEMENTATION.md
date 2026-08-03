# Implementation

English · [简体中文](zh-CN/IMPLEMENTATION.md)

## From configuration to a worker

1. `configure.py` selects a preset or accepts a custom manifest and stores a validated copy in a stable user directory.
2. `load_manifest()` rejects unknown protocols, invalid URLs, inline credentials, duplicate IDs, and incomplete selection.
3. Configure checks macOS Keychain, Windows Credential Manager, or environment references. A missing credential stops the workflow with exit code 3 before runtime files are installed.
4. The installer renders one catalog and agent TOML per model and one `config.toml` provider block per provider.
5. It writes `subagent-selection.json`, mapping model IDs to agent, provider, remote model, and context declarations.
6. For `upstream_protocol=anthropic_messages`, it derives a local adapter and installs a macOS LaunchAgent or Windows Task Scheduler task bound to that provider identity.
7. Configure runs doctor. The user then starts a new Codex task, where the Skill calls `delegation_runtime.py begin` to resolve the single `active.agent`.
8. `begin` performs an authentication preflight in the same process context that will delegate the workers. It checks the declared environment reference or invokes the installed credential helper without exposing the credential value. If the lookup fails, it returns `auth_unavailable` before any worker is started.

## Why task bodies use files

In some Codex Desktop and provider/protocol combinations, the native parent-to-child message does not reliably carry the full subtask body. A worker may otherwise receive an empty task or only an encrypted block.

The parent writes the complete handoff before spawning. The protocol header remains `# DeepSeek task handoff v1` for compatibility; it does not require a DeepSeek model.

```text
validate real cwd
  → lock the mailbox
  → validate pending header and task ID
  → atomically rename to claimed/<task_id>--<claim_id>.md
  → persist receipt
  → return task_id, claim_id, path, and receipt
```

The receipt stores an attempt ID, timestamps, status, exit code, and optional explicit parent/worker thread, agent, model, and provider metadata. `complete` and `fail` update only the exact `task_id + claim_id` attempt.

A follow-up updates the claimed file before waking the same worker. The parent never reconstructs a claimed path with an ambiguous glob.

## Fallback state machine

Each `runs/<run-id>.json` stores:

- current model and agent;
- attempted models;
- generation, starting at 1;
- switches used and `max_switches`;
- failure classification and final outcome.

The parent calls `record-failure` only after provider transport retries are exhausted. Before switching, it confirms every old worker stopped. Completed work stays accepted; only incomplete claims are recovered. Accepted runs finish with `--outcome completed`, while an ineligible or exhausted failure finishes as `blocked`.

Authentication failures are intentionally not fallback-eligible. A missing bearer credential is a local configuration/context problem, not a transient upstream failure. Reinject the credential and rerun `begin` in the same Windows user context; do not bypass the loopback adapter's bearer check.

## Atomic installation and rollback

Managed files are written to a same-directory temporary file, flushed, and atomically replaced. Changed targets receive a timestamped backup. If a newly installed adapter fails to start, service management stops the new job and removes only the matching definition it created.

Resource digests prevent two unsafe behaviors:

- Uninstall does not delete a managed file after the user changes it.
- Removing one manifest does not delete a shared Skill or adapter still owned by another installation.

A provider ID with drifted URL, authentication, protocol, or adapter settings fails instead of silently reusing the wrong upstream configuration.

## Adapter identity and audit

The health response includes `service_id`, adapter type, and a fingerprint bound to the provider ID and upstream URL. Install and doctor therefore reject an old adapter or unrelated process that happens to answer on the expected port.

Audit JSONL contains request ID, model, requested/effective token limit, status, error category, duration, and upstream usage. It excludes prompts and credentials.

## Worker tool boundary

External-model workers retain local repository tools such as command execution, stdin, planning, and freeform patch application. They do not receive:

- subagent creation, which prevents recursive delegation;
- `request_user_input`, because blockers return to the parent;
- built-in web search or image tools, which have no current cross-provider mapping.

## Verify the actual context window

Catalog `context_window` values are declarations. The Desktop client, model catalog, or service may enforce a smaller runtime window.

```bash
python3 scripts/session_audit.py \
  --rollout ~/.codex/sessions/YYYY/MM/DD/rollout-....jsonl \
  --selection ~/.codex/models/subagent-selection.json
```

The audit reports declared values beside `task_started.model_context_window`, follow-ups, output tokens, and native errors. One existing catalog declared 1M while the observed Desktop window was `258400`; do not present the catalog value as an observed runtime limit.
