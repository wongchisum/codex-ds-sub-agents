# Skill 迁移

项目和插件仍叫 `codex-custom-subagents`。面向 Prompt 的 Skill 已从 `$deepseek-delegation` 改为 `$codex-custom-agents`，新安装路径是 `~/.codex/skills/codex-custom-agents/`。

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

1. 查找 `~/.codex/skills/deepseek-delegation/`。
2. 读取旧安装的 `.codex-deepseek-manifest.json` 所有权清单。
3. 只移除摘要仍匹配的受管理文件。
4. 保留用户修改文件、未知文件、符号链接和未受管理的旧目录。
5. 安装 `codex-custom-agents`，并重新渲染 agent 路径。

迁移可重复运行。未受管理的旧目录不会被强制删除，需要用户检查并自行决定如何处理。

## 不再兼容的入口

迁移后不提供 `$deepseek-delegation` 同名别名。文档、Prompt 和新任务统一使用 `$codex-custom-agents`。保留两个并行 Skill 会让 Codex 选择错误入口，也会使后续卸载所有权不清晰。

`.deepseek-delegations/`、`# DeepSeek task handoff v1` 和 `.codex-deepseek-manifest.json` 暂不改名。前两项属于现有任务文件协议，最后一项用于识别旧安装所有权；直接改名会破坏未完成任务或使安全卸载失去依据。

## 任务缓存

安装或迁移完成后必须关闭当前 Codex 任务并新建任务。旧任务缓存的 agent 注册表不会热更新，可能返回 `unknown agent_type`。该错误不是模型网络故障，不能触发 fallback。

## 第一版模型配置

旧 `deepseek_worker` agent、旧 provider 和旧 Keychain service 可以由安装器识别，但新配置推荐使用 schema v2 manifest。`--profile` 仍供旧脚本兼容；新的用户流程使用 `--primary` 和独立的 `--fallback`。

远端仓库改名后的旧 clone 可手工更新 remote：

```bash
git remote set-url origin https://github.com/wongchisum/codex-custom-subagents.git
git remote -v
```
