English · [简体中文](zh-CN/TESTING.md)

# Testing and acceptance

## Automated tests

```bash
python3 -m unittest discover -s tests -v
python3 tests/test_release_assets.py
python3 -m compileall -q scripts skills/codex-custom-subagents/scripts
```

The suite covers configuration, manifest validation, selection and fallback,
install ownership, protected uninstall, protocol conversion, SSE, the adapter
service, atomic task claiming and recovery, session audits, and release assets.
Python 3.9 skips release checks that require `tomllib`; CI uses Python 3.11 on
both `macos-latest` and `windows-latest`.

## Installation checks

```bash
python3 scripts/doctor.py \
  --manifest ~/.codex/custom-subagents/manifests/deepseek-anthropic.json
```

A PASS confirms local configuration, credential references, and service
identity. It does not prove that the provider is reachable, the account has
model access or credit, the remote model name is accepted, or a worker can
finish a real tool loop.

## Codex Desktop acceptance

Start a new Codex task after every agent installation or switch, then verify:

1. The parent reads `subagent-selection.json` and reports the selected primary,
   agent, and provider.
2. The run creates a pending task with a unique task ID.
3. The selected worker starts with `fork_turns: "none"`.
4. The worker reports its task ID, claim ID, claimed path, and receipt.
5. The worker reads a real project file, runs a read-only command, and completes
   a small tool loop.
6. The worker completes its claim; the parent confirms a `completed` receipt and
   exit code 0.
7. The parent validates real artifacts or test output instead of accepting a
   text-only success claim.
8. `session_audit.py` confirms the agent, model, provider, and observed context
   window.

## Fallback acceptance

Use controlled failures that are eligible for fallback. Authentication errors
and invalid model names must not be treated as transport failures.

1. Confirm the entire first batch uses the primary worker.
2. Exhaust the primary with `network`, `timeout`, `rate_limit`, `billing`, or
   `service_unavailable` failures.
3. Stop every worker from the failed generation.
4. Confirm `record-failure` returns `status: switched`, increments the
   generation, and selects the next agent.
5. Resume only incomplete claims and confirm the replacement batch uses one
   fallback agent.
6. Close the run after success and confirm it does not return to an attempted
   model.

Also verify that `auth`, `invalid_request`, `model_not_found`, and
`task_failure` return blocked without switching.

## Current evidence and open verification

See the [current test report](../TEST_REPORT.md) for recorded evidence. Until a
new real run is recorded, these claims remain unverified:

- A native 1M-context Codex Desktop subagent; one recorded session reported
  `258400`.
- A high-cost request near the declared 1M-token boundary.
- A real Claude-to-Gemini fallback run.
- Real billing, rate-limit, and network failure classification.
- Long-run stability across repeated compaction.

Record the date, operating system, Python and Codex versions, manifest, agent,
remote model, task ID, receipt, original error, observed context window, and
diagnostic archive name. Never record API keys or prompt secrets. Follow the
[Windows real-machine checklist](WINDOWS_TESTING.md) for Windows releases.
