---
name: codex-custom-subagent
description: Delegate bounded tasks to configured external-model workers in Codex Desktop through an atomic workspace mailbox. Use for legacy deepseek_worker agents and manifest-selected model workers whose native task messages may not be visible to the provider.
---

# Custom Subagent Delegation

The skill ID is `codex-custom-subagent`. The legacy `deepseek-delegation`,
`codex-custom-agents`, and `codex-custom-subagents` install paths are migrated by
`scripts/migrate_skill.py`
and are never installed as second live skills. The selected worker may use
DeepSeek, Claude, Gemini, or another configured model.

Use the native subagent channel for lifecycle and results. Use a workspace task pool for task bodies because an external model may not receive the native message or task name.

## Optional configured model selection

When `$CODEX_HOME/models/subagent-selection.json` exists, it defines one primary
model and ordered fallbacks. Read it before creating workers:

1. Before spawning, create one durable run state:
   `delegation_runtime.py --workspace . begin --run-id <unique_run_id> --selection "$CODEX_HOME/models/subagent-selection.json"`.
   Use the returned `active.agent`; do not independently reconstruct model bindings.
   If it returns `code: auth_unavailable`, stop before creating any worker,
   report the credential/context blocker, and rerun `begin` only after the
   credential has been made available in the same Codex process context.
2. Resolve `selection.primary` to `models.<id>.agent` only when auditing the
   runtime output.
3. Create every worker in the batch with that same agent type. Never mix primary
   and fallback workers in one running batch.
4. Provider transport retries happen inside Codex. After they are exhausted,
   record the native error with `delegation_runtime.py --workspace . record-failure --run-id <run_id>`
   and the available `--http-status`,
   `--error-code`, or `--message`. Switch only when its JSON result has
   `status: switched`.
5. The runtime permits switching only when the exhausted failure is classified as `network`,
   `timeout`, `rate_limit`, `billing`, or `service_unavailable`.
6. Do not switch for `auth`, `invalid_request`, `model_not_found`, `task_failure`,
   or an unknown failure. Report the blocker instead.
7. Before switching, confirm every worker using the old model has stopped. Keep
   completed work and recover only incomplete claims, then spawn all replacement
   workers with the exact `active.agent` returned by the runtime. The persisted
   run state prevents returning to an attempted model or exceeding `max_switches`.
8. After accepting all results, close the run with `delegation_runtime.py --workspace . finish --run-id <run_id> --outcome completed`.
   If an ineligible
   or exhausted failure blocks the batch, use `--outcome blocked` instead.

If the selection file is absent, use the legacy DeepSeek-only procedure below.

## Create a task pool

The pool always lives at the calling thread's real cwd: `claim_task.py --workspace .` resolves the pool root (`./.deepseek-delegations`) through every symlink against the process cwd, so threads sharing a logical path share one pool. The command rejects a workspace that differs from the real cwd. Use `--allow-workspace-mismatch` only for deliberate legacy compatibility. The project a task operates on is independent of the pool location and is given as an absolute path inside the task body.

1. Split the request into independent bounded tasks. Give each task a unique ID of 1-64 lower-case letters, digits, and underscores (`[a-z0-9_]{1,64}`).
2. Check `.deepseek-delegations/pending/` before creating a batch. Do not mix unexplained pending tasks into a new batch.
3. Write every complete task to `.deepseek-delegations/pending/<task_id>.md` before spawning workers.
4. Start each task file with `# DeepSeek task handoff v1`, followed by a blank line and `Task: <task_id>`. Only the header block (the first lines of the file) is validated; `Task:`-looking lines inside the task body are ignored. Include the full scope, restrictions, output, and verification.
5. Choose the agent type once for the whole batch: when `subagent-selection.json` exists, use the agent resolved from its primary model; otherwise use the legacy `deepseek_worker`. Spawn one selected agent per pending task, up to the available agent slots. Use `fork_turns: "none"` and generic unique worker names such as `subagent_pool_1`. Never mix agent types in one batch, and do not assume a worker name determines which task it claims.
6. Wait for every worker. Map each worker to the Task ID and claimed path reported in its result, then inspect the actual artifacts and verification.
7. If more tasks remain than available slots, spawn the next wave only after a slot becomes free.

Each worker atomically moves one file from `pending/` to `claimed/`; simultaneous workers cannot claim the same file. Every successful claim also writes a durable receipt (`claimed/<task_id>--<claim_id>.md.receipt`) before reporting, so a crash between the rename and the stdout delivery never loses the claim silently. Receipt schema v1 records `schema_version`, a unique `attempt_id`, `claimed_at`, and explicit nullable `parent_thread_id`, `worker_thread_id`, `agent`, `model`, and `provider` fields. Pass those metadata flags only when their values are known; missing values stay `null` and must never be inferred.

## Finish a claim

Before a worker's final response, record the exact attempt with both remembered identifiers:

- Success: `claim_task.py --workspace . complete --task-id <task_id> --claim-id <claim_id> --exit-code 0`
- Failure: `claim_task.py --workspace . fail --task-id <task_id> --claim-id <claim_id>`; add the original nonzero `--exit-code` only when one exists.

Both commands atomically update the durable receipt, retain its original claim identity and metadata, and record `completed_at`, `status`, `exit_code`, and an optional bounded `summary`. Never put secrets or full logs in `summary`. Recording `fail` successfully does not erase or replace the delegated task's original failure; the worker must still report that failure.

## Follow up

Locate the worker's claimed file deterministically: use the exact `path` from the claim response, or run `claim_task.py --workspace . locate --task-id <task_id>` (add `--claim-id <claim_id>` to pin the exact claim). Never reconstruct the claimed path with your own glob over `claimed/<task_id>--*.md`: an ambiguous match means the claim state is broken and must be resolved with `recover` first. Update that claimed file first, then wake the same worker with `followup_task`. Require the worker to reread its remembered claimed path before continuing.

Keep claimed files until the parent accepts the results. Never put secrets in a task file. If a worker reports an empty queue, do not ask it to infer work from unrelated workspace files.

## Recover lost claims

When a worker dies or its result is never received:

1. Run `claim_task.py --workspace . recover --dry-run` to classify every claim (would-requeue / would-reject / would-clean / skipped, with reasons).
2. `recover` never guesses that a running task is dead. Claims carrying a receipt are treated as possibly running and are only requeued when you confirm with `--task-id <task_id>` or `--claim-id <claim_id>`, or with `--all` after confirming that no workers are still running.
3. Default requeue goes to `recovered/` (keeps claim identity); `--to pending` re-queues for the next worker and fails with a structured error on a name collision instead of overwriting. Recovery marks and retains the old receipt, so a subsequent claim gets a new `claim_id` and `attempt_id` without destroying the earlier attempt audit.
4. Deterministic cleanup never deletes a successful-attempt receipt. An orphan receipt is marked and retained for audit, while a claimed file whose protocol header is invalid is moved to `rejected/` and any valid receipt records that terminal state.
5. After requeueing, spawn a replacement worker for each recovered task. A claimed file without a receipt is not requeued automatically; confirm it explicitly before re-queuing.
