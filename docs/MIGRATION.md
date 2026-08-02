# Skill migration

English · [简体中文](zh-CN/MIGRATION.md)

The project, plugin, and active Skill are all named `codex-custom-subagents`. Older releases used `$deepseek-delegation`, followed by `$codex-custom-agents`. New installs use `~/.codex/skills/codex-custom-subagents/`.

## Managed migration

macOS:

```bash
python3 scripts/migrate_skill.py --dry-run
python3 scripts/migrate_skill.py
```

Windows:

```powershell
py -3 scripts\migrate_skill.py --dry-run
py -3 scripts\migrate_skill.py
```

The migrator:

1. Looks for `~/.codex/skills/deepseek-delegation/` and `~/.codex/skills/codex-custom-agents/`.
2. Reads each old `.codex-deepseek-manifest.json` ownership record.
3. Removes only managed files whose digest still matches.
4. Preserves modified files, unknown files, symlinks, and unmanaged directories.
5. Installs `codex-custom-subagents` and re-renders registered agent paths.

Migration is idempotent. An unmanaged old directory is reported and preserved for manual review.

## Compatibility boundary

New prompts and tasks must use `$codex-custom-subagents`; no active alias is installed for either legacy name.

The names `.deepseek-delegations/`, `# DeepSeek task handoff v1`, and `.codex-deepseek-manifest.json` intentionally remain unchanged. They identify the existing mailbox protocol and old ownership records. Renaming them would break in-flight tasks or safe uninstall detection.

## Start a new Codex task

After installation or migration, close the current Codex task and start a new task. An open task keeps the agent registry loaded at task creation and can return `unknown agent_type`. This is a local task-lifecycle error, not an eligible model fallback failure.

## Old repository clones

An old clone can update its remote explicitly:

```bash
git remote set-url origin https://github.com/wongchisum/codex-custom-subagents.git
git remote -v
```
