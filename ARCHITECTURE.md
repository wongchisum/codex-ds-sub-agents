# Architecture

本文描述仓库当前实现和已验证行为，不把 Codex 的自定义 agent、model catalog 或 provider 配置当作稳定的公开兼容承诺。

## 组件

- `config/deepseek-provider.toml`：DeepSeek provider 配置片段。
- `models/deepseek-v4-flash.json`：Codex 模型能力目录。
- `agents/deepseek-worker.toml.template`：安装时渲染绝对路径的子智能体配置。
- `skills/deepseek-delegation/`：指导主智能体建立任务池。
- `skills/deepseek-delegation/scripts/claim_task.py`：原子领取一个 pending 任务；提供 `recover`（孤儿恢复）与 `locate`（确定性定位）子命令。
- `scripts/install.py`：安装文件并合并 provider 配置。
- `scripts/doctor.py`：检查安装文件、Keychain 和 Codex 配置。
- `scripts/uninstall.py`：只删除未被用户修改的项目安装文件，并在修改 `config.toml` 前备份。
- `assets/codex-deepseek-subagents.png`：README 使用的本地测试截图。

安装器在 skill 目录写入 `.codex-deepseek-manifest.json`。升级时只清理旧清单记录、已从新版本移除且内容仍与旧摘要一致的文件；未知文件和用户修改过的文件保留。

## 数据流

```text
User prompt
  → parent Codex splits bounded tasks
  → pending/<task_id>.md
  → N native deepseek_worker threads
  → atomic rename pending → claimed（rename 前先原子写入 .receipt 票据）
  → DeepSeek reads only its claimed file
  → workspace changes + native final response
  → parent maps worker ↔ Task ID and verifies
  → recover 只在父智能体确认 worker 已死后重排队
```

原子性来自同一文件系统内的 `rename`。两个进程同时选择同一个文件时，只有一个进程能成功移动；另一个进程捕获 `FileNotFoundError` 后尝试下一个任务。领取脚本先原子写入 claim 票据（`.receipt`）再执行 rename，因此 rename 成功后只剩一次 stdout 打印，崩溃窗口最小化；stdout 丢失时父智能体可凭票据/`recover`/`locate` 确定性恢复。

## 任务池位置

任务池必须位于调用线程的真实 cwd：`claim_task.py` 把 `--workspace` 展开并解析所有符号链接（`Path.resolve()`），相对路径按调用进程真实 cwd 解析，同一逻辑路径的多个线程共享同一个池。被操作的项目与任务池无关，在任务正文中以绝对路径给出。

## 信箱协议

路径：

```text
.deepseek-delegations/pending/<task_id>.md
.deepseek-delegations/claimed/<task_id>--<claim_id>.md
.deepseek-delegations/claimed/<task_id>--<claim_id>.md.receipt
.deepseek-delegations/rejected/<task_id>--<claim_id>.md
.deepseek-delegations/recovered/<task_id>--<claim_id>.md
```

格式：

```md
# DeepSeek task handoff v1

Task: task_id

完整任务正文
```

`task_id` 只允许小写字母、数字和下划线，最长 64 字符。校验只检查协议头部（首行 + 紧随其后的元数据块）：`Task:` 行必须恰好出现一次且与文件名一致；任务正文中出现 `Task:` 开头的行不会导致误拒绝。`pending/` 中的目录、符号链接和非法 `task_id` 文件被跳过并输出结构化诊断，不会被当作任务领取。空池不创建任何目录。

## 领取与恢复

- 领取成功输出 `{"status": "claimed", "task_id": ..., "claim_id": ..., "path": ..., "receipt": ...}`，exit 0；票据与 stdout 内容一致。
- `recover` 不猜测仍在执行的任务：带票据的 claimed 文件只有在父智能体显式确认（`--task-id`/`--claim-id`/`--all`）后才重排队；孤儿票据（无对应 claimed 文件）与协议头非法的 claimed 文件自动清理/驳回。`--dry-run` 只报告不移动。
- `locate --task-id <id>` 确定性定位 claimed 文件，重复时返回 `ambiguous`（exit 3），要求先 `recover` 再跟进；`--claim-id` 可精确定位。
- rejected 或恢复过程中的移动失败返回结构化 JSON 错误，不输出 traceback。

## 已知限制

1. Codex 没有把原生 worker 名称可靠地暴露给 DeepSeek，因此任务由 worker 池动态领取。
2. 主智能体必须在创建 worker 前写完全部 pending 文件。
3. follow-up 必须先更新 claimed 文件，再唤醒原 worker；定位用 `locate` 或领取响应的精确路径，禁止自行 glob。
4. worker 异常退出后，claimed 任务不会自动重排队；需要主智能体确认 worker 已死后用 `recover` 显式恢复。脚本不会猜测仍在执行的任务。
5. 任务文件是工作区明文，不得放置密钥、访问令牌或用户隐私数据。
6. 当前认证模板依赖 macOS Keychain。

## 后续演进

- 增加 Linux/Windows 凭证适配器。
- 增加端到端 Desktop 多 worker 测试工具。
- 若 Codex 将明文 task ID 暴露给自定义 provider，可恢复 worker 与任务的确定性绑定并简化任务池。
