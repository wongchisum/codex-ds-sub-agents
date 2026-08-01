---
name: deepseek-delegation
description: Delegate one or more bounded tasks to native DeepSeek workers in Codex Desktop through an atomic workspace mailbox. Use whenever spawning, retrying, following up, or running parallel deepseek_worker subagents whose native task messages may not be visible to the external provider.
---

# DeepSeek Delegation

Use the native subagent channel for lifecycle and results. Use a workspace task pool for task bodies because DeepSeek may not receive the native message or task name.

## Create a task pool

The pool always lives at the calling thread's real cwd: `claim_task.py --workspace .` resolves the pool root (`./.deepseek-delegations`) through every symlink against the process cwd, so threads sharing a logical path share one pool. The project a task operates on is independent of the pool location and is given as an absolute path inside the task body.

1. Split the request into independent bounded tasks. Give each task a unique ID of 1-64 lower-case letters, digits, and underscores (`[a-z0-9_]{1,64}`).
2. Check `.deepseek-delegations/pending/` before creating a batch. Do not mix unexplained pending tasks into a new batch.
3. Write every complete task to `.deepseek-delegations/pending/<task_id>.md` before spawning workers.
4. Start each task file with `# DeepSeek task handoff v1`, followed by a blank line and `Task: <task_id>`. Only the header block (the first lines of the file) is validated; `Task:`-looking lines inside the task body are ignored. Include the full scope, restrictions, output, and verification.
5. Spawn one `deepseek_worker` per pending task, up to the available agent slots. Use `fork_turns: "none"` and generic unique worker names such as `deepseek_pool_1`. Do not assume a worker name determines which task it claims.
6. Wait for every worker. Map each worker to the Task ID and claimed path reported in its result, then inspect the actual artifacts and verification.
7. If more tasks remain than available slots, spawn the next wave only after a slot becomes free.

Each worker atomically moves one file from `pending/` to `claimed/`; simultaneous workers cannot claim the same file. Every successful claim also writes a durable receipt (`claimed/<task_id>--<claim_id>.md.receipt`) before reporting, so a crash between the rename and the stdout delivery never loses the claim silently.

## Follow up

Locate the worker's claimed file deterministically: use the exact `path` from the claim response, or run `claim_task.py --workspace . locate --task-id <task_id>` (add `--claim-id <claim_id>` to pin the exact claim). Never reconstruct the claimed path with your own glob over `claimed/<task_id>--*.md`: an ambiguous match means the claim state is broken and must be resolved with `recover` first. Update that claimed file first, then wake the same worker with `followup_task`. Require the worker to reread its remembered claimed path before continuing.

Keep claimed files until the parent accepts the results. Never put secrets in a task file. If a worker reports an empty queue, do not ask it to infer work from unrelated workspace files.

## Recover lost claims

When a worker dies or its result is never received:

1. Run `claim_task.py --workspace . recover --dry-run` to classify every claim (would-requeue / would-reject / would-clean / skipped, with reasons).
2. `recover` never guesses that a running task is dead. Claims carrying a receipt are treated as possibly running and are only requeued when you confirm with `--task-id <task_id>` or `--claim-id <claim_id>`, or with `--all` after confirming that no workers are still running.
3. Default requeue goes to `recovered/` (keeps claim identity); `--to pending` re-queues for the next worker and fails with a structured error on a name collision instead of overwriting.
4. Deterministic cleanup happens without confirmation: orphan receipts (receipt without its claimed file) are removed, and claimed files whose protocol header is invalid are moved to `rejected/`.
5. After requeueing, spawn a replacement worker for each recovered task. A claimed file without a receipt is not requeued automatically; confirm it explicitly before re-queuing.
