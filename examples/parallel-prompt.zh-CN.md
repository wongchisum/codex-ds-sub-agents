[English](parallel-prompt.md) · 简体中文

# 自定义 subagent 并行 Prompt

```text
使用 $codex-custom-subagents，把以下独立任务放入同一个任务池。先运行 delegation_runtime.py begin，从 subagent-selection.json 解析 active.agent；如果 selection 不存在，才回退到 deepseek_worker。整批 worker 使用同一个 agent，全部设置 fork_turns: "none"。主智能体只负责拆分、等待和最终验收，不得代做子任务。

任务一，ID 为 analyze_auth：
分析 src/auth 的调用关系和错误边界，不修改代码，报告文件与行号证据。

任务二，ID 为 test_users：
运行用户模块测试，定位失败根因；只允许修改 tests/users，完成后重新运行相关测试。

验收要求：
1. 每个子智能体报告不同的 Task ID 和 claimed 文件路径。
2. 每个子智能体只执行自己领取的任务。
3. 主智能体检查真实文件和测试输出，并报告 worker 与 Task ID 的对应关系。
4. 同一批次不得混用 primary 与 fallback；发生符合规则的失败时，先停止旧 worker，再整批切换。
```
