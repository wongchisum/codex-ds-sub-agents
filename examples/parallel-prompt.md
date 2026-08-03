English · [简体中文](parallel-prompt.zh-CN.md)

# Custom subagent parallel prompt

```text
Use $codex-custom-subagents to put the independent tasks below into one task
pool. First run delegation_runtime.py begin and resolve active.agent from
subagent-selection.json; fall back to deepseek_worker only when the selection
file does not exist. Use one agent for the entire worker batch and set
fork_turns: "none" for every worker. The parent agent owns task splitting,
waiting, and final acceptance.

Task one, ID analyze_auth:
Analyze call relationships and error boundaries under src/auth. Do not modify
code. Report file and line evidence.

Task two, ID test_users:
Run the user-module tests and identify the root cause. You may edit only
tests/users. Re-run the relevant tests after the change.

Acceptance:
1. Each subagent reports a unique task ID and claimed file path.
2. Each subagent performs only its claimed task.
3. The parent inspects real files and test output, then reports the worker-to-task mapping.
4. Never mix primary and fallback workers in one batch. On an eligible failure,
   stop the old workers before switching the entire batch.
```
