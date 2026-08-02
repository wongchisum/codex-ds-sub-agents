# Codex Custom Subagents

[English](README_EN.md) · [文档索引](docs/README.md) · [安装](docs/INSTALLATION.md) · [配置](docs/CONFIGURATION.md) · [排错](docs/TROUBLESHOOTING.md)

`codex-custom-subagents` 是一个 Codex Desktop 插件。它允许用户配置自定义 `base_url`、模型名、凭证引用和上游协议，并把选中的模型作为 sub-agent 使用。插件提供的 Skill 名为 `$codex-custom-agents`。

![Codex Desktop 中的自定义子智能体任务](assets/codex-custom-subagents.png)

## 能力

- Provider 按 `openai_responses` 或 `anthropic_messages` 协议抽象，不把协议写死在模型名中。
- 一次运行只使用一个 primary 模型；网络、限流等合格故障发生后，父任务按有序 fallback 整批切换。
- 支持自定义 URL、远端模型名、上下文声明、输出上限和安全凭证引用。
- Anthropic Messages 通过本机 Responses adapter 接入 Codex。
- 支持 macOS Keychain、Windows Credential Manager 和环境变量凭证引用；manifest 禁止内联 Key。
- 提供旧 `$deepseek-delegation` 安装的安全迁移、Windows Task Scheduler 服务和脱敏诊断包。

## 快速安装

要求 Python 3.9+、Git、已登录的 Codex Desktop，以及 Codex `0.146.0` 或更高版本。Windows 使用 `py -3`；macOS/Linux 使用 `python3`。

```bash
git clone https://github.com/wongchisum/codex-custom-subagents.git
cd codex-custom-subagents
python3 scripts/configure.py --list-model-protocols
python3 scripts/configure.py --primary deepseek-anthropic
```

内置选择：

- `deepseek-anthropic`：deepseek (anthropic)
- `deepseek-openai`：deepseek (openai)
- `gemini-anthropic`：gemini (anthropic)
- `claude-anthropic`：claude (anthropic)

fallback 单独配置，不属于 primary profile：

```bash
python3 scripts/configure.py \
  --primary claude-anthropic \
  --fallback gemini-anthropic \
  --fallback deepseek-openai
```

脚本不会接收或保存凭证值。缺少凭证时会以 exit 3 停止，并打印当前操作系统的交互式保存命令。保存后重复完全相同的配置命令。

安装或迁移完成后必须关闭当前 Codex 任务并新建任务。agent 注册表不会在已打开的任务中热更新；旧任务可能返回 `unknown agent_type`。

## 从旧 Skill 迁移

```bash
python3 scripts/migrate_skill.py --dry-run
python3 scripts/migrate_skill.py
```

迁移器只删除旧安装清单拥有且摘要未变化的文件。用户修改过的文件、未知文件和符号链接会保留。迁移后只使用 `$codex-custom-agents`，不再使用 `$deepseek-delegation`。完整规则见[迁移文档](docs/MIGRATION.md)。

## 让 Codex 帮你安装和配置

直接把[安全安装 Prompt](docs/PROMPT_INSTALLATION.md)发送给 Codex。Prompt 会要求 Codex 分开询问 primary 和 fallback，并在需要凭证时暂停，让用户在本机终端交互输入。

## 使用

```text
使用 $codex-custom-agents，把代码审查和测试分析拆成两个独立任务。
读取当前 subagent-selection.json，整批使用 active.agent；不要在同一批次混用 primary 和 fallback。
```

目标项目的 `.gitignore` 应包含：

```gitignore
/.deepseek-delegations/
```

任务信箱目录暂时保留旧名称，作为任务文件协议的兼容边界；这不代表 Skill 仍叫 `deepseek-delegation`。

## 文档

安装、架构、模型适配、实现原理、迁移、测试和 Windows 真机步骤统一收录在[文档索引](docs/README.md)。如果希望让 Codex 代为安装，可直接使用[安装 Prompt](docs/PROMPT_INSTALLATION.md)。

`doctor.py` 只验证安装、凭证条目和 adapter 健康，不会调用真实模型或检查余额。Windows 自动化测试已经覆盖平台分支；Task Scheduler 与 Credential Manager 仍需按[真机清单](docs/WINDOWS_TESTING.md)验收。

本项目是第三方集成，不是 OpenAI、Anthropic、Google 或 DeepSeek 的官方产品。
