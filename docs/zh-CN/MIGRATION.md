[English](../MIGRATION.md) · 简体中文

# Skill 迁移

项目、插件和面向 Prompt 的 Skill 现在统一叫 `codex-custom-subagents`。旧版本曾使用 `$deepseek-delegation`，中间版本使用过 `$codex-custom-agents`；新安装路径统一为 `~/.codex/skills/codex-custom-subagents/`。

## 自动迁移

在仓库根目录运行：

```bash
python3 scripts/migrate_skill.py --dry-run
python3 scripts/migrate_skill.py
```

Windows 使用：

```powershell
py -3 scripts\migrate_skill.py --dry-run
py -3 scripts\migrate_skill.py
```

迁移器会：

1. 查找 `~/.codex/skills/deepseek-delegation/` 和 `~/.codex/skills/codex-custom-agents/`。
2. 读取旧安装的 `.codex-deepseek-manifest.json` 所有权清单。
3. 只移除摘要仍匹配的受管理文件。
4. 保留用户修改文件、未知文件、符号链接和未受管理的旧目录。
5. 安装 `codex-custom-subagents`，并重新渲染 agent 路径。

迁移可重复运行。未受管理的旧目录不会被强制删除，需要用户检查并自行决定如何处理。

## 不再兼容的入口

迁移后不提供 `$deepseek-delegation` 或 `$codex-custom-agents` 别名。文档、Prompt 和新任务统一使用 `$codex-custom-subagents`。保留多个并行 Skill 会让 Codex 选择错误入口，也会使后续卸载所有权不清晰。

新批次使用 `.codex-custom-subagents/` 和
`# Codex Custom Subagents task handoff v1`。程序不会自动移动旧
`.deepseek-delegations/`：进行中的文件锁和 receipt 内的绝对路径让自动改名存在风险。

开始新批次前检查两个目录。如果旧目录仍有未完成任务，先停止所有旧 worker，
再给完成或恢复该批次所需的 `claim_task.py`、`delegation_runtime.py` 命令增加
`--legacy-mailbox`。该选项必须放在子命令前，例如
`claim_task.py --workspace . --legacy-mailbox recover --dry-run`。不得向旧目录写入
新任务，也不得合并两个信箱。旧
`# DeepSeek task handoff v1` 协议头只为这条显式兼容路径保留读取能力。

`.gitignore` 必须同时保留两个信箱路径，避免包含私有仓库上下文的任务正文进入
Git。`.codex-deepseek-manifest.json` 继续保持旧名，因为安全清理旧 Skill 安装时
仍靠它判断文件所有权。

## 任务缓存

安装或迁移完成后必须关闭当前 Codex 任务并新建任务。旧任务缓存的 agent 注册表不会热更新，可能返回 `unknown agent_type`。该错误不是模型网络故障，不能触发 fallback。

## 第一版模型配置

旧 `deepseek_worker` agent、旧 provider 和旧 Keychain service 可以由安装器识别，但新配置推荐使用 schema v2 manifest。`--profile` 仍供旧脚本兼容；新的用户流程使用 `--primary` 和独立的 `--fallback`。

远端仓库改名后的旧 clone 可手工更新 remote：

```bash
git remote set-url origin https://github.com/wongchisum/codex-custom-subagents.git
git remote -v
```
