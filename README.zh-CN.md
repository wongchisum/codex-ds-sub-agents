# Codex Custom Subagents

[English](README.md) · 简体中文

[文档](docs/zh-CN/README.md) · [安装](docs/zh-CN/INSTALLATION.md) · [配置](docs/zh-CN/CONFIGURATION.md) · [故障排查](docs/zh-CN/TROUBLESHOOTING.md)

`codex-custom-subagents` 帮助 Codex Desktop 使用自定义模型 Provider 作为 subagent 完成任务。它支持 macOS 和 Windows，不把凭证写入 Prompt 或 manifest，并通过 `$codex-custom-subagents` Skill 让 Codex 委派工作。

![Codex Desktop 中的自定义 subagent 任务](assets/codex-custom-subagents.png)

## 让 Codex 安装

在 Codex Desktop 中打开本仓库，然后发送下面的 Prompt：

```text
请从这个仓库安装并配置 Codex Custom Subagents：
https://github.com/wongchisum/codex-custom-subagents

1. 检查操作系统、Python 解释器、Codex 版本和 Git。Windows 使用 py -3，
   macOS 使用 python3。
2. 修改文件前阅读 README.md、docs/INSTALLATION.md、
   docs/CONFIGURATION.md 和 docs/MIGRATION.md。
3. 检查是否存在受管理的旧 Skill：deepseek-delegation 或
   codex-custom-agents。删除受管理文件前先预览迁移，并保留用户修改过的文件和未知文件。
4. 让我选择一个 primary 模型/协议组合，再单独询问是否需要有序 fallback。
5. 只收集 Provider URL、远端模型名、协议、上下文声明、输出上限和凭证引用名。
   不要让我把 API Key 粘贴到 Codex。
6. 按我的选择运行明确的 configure.py 命令。如果命令以 exit 3 退出，展示安全的
   交互式凭证命令，然后等待我在本机执行。
7. 凭证保存后重复相同的 configure 命令，再运行 doctor。doctor 未通过时不得宣称完成。
8. 提醒我关闭当前 Codex 任务并新建任务，因为已打开的任务不会热加载新 agent 类型。
9. 在新任务中使用 $codex-custom-subagents，不再使用两个旧 Skill 名。
10. 不显示、复制、提交或上传任何凭证值。
```

独立版本也收录在 [docs/zh-CN/PROMPT_INSTALLATION.md](docs/zh-CN/PROMPT_INSTALLATION.md)。

## 在 Codex 中使用

安装后新建一个 **Codex 任务**，发送类似 Prompt：

```text
使用 $codex-custom-subagents，把下面的工作委派给已经配置的自定义 subagent：

<描述任务>

读取 subagent-selection.json，整批使用 active.agent，检查 worker 产物和测试，
只汇报父任务已经验收的结果。
```

需要并行处理多个独立任务时：

```text
使用 $codex-custom-subagents，把这项工作拆成相互独立的 subagent 任务。
整批只使用一个 active model，为每个 worker 指定有限职责，并在接受结果前逐项验证。
```

## 它实际做什么

这个插件会把自定义 Provider 安装成 Codex Desktop 可调用的 agent。它不替代
Codex，也不是一个通用聊天界面。父任务负责拆分和验收，自定义 subagent 在同一
工作区内使用 Codex 提供的文件、命令和补丁工具完成具体工作。

```text
用户任务
  → 父 Codex 读取配置并选择 primary agent
  → 把有限职责的任务写入 .codex-custom-subagents/pending/
  → 多个自定义 subagent 原子领取不同任务
  → worker 分析或修改文件，并运行命令和测试
  → 父 Codex 检查产物后决定是否接受
  → 符合条件的传输故障可让整批切换到 fallback agent
```

实际用途包括：

- 把仓库审查拆成架构、安全、测试和文档等相互独立的任务。
- 把长代码调查或一个边界明确的实现交给指定的自定义模型。
- 并行处理多个修复，父任务统一检查每份 diff 和测试结果。
- 为 primary 和有序 fallback 配置不同 Provider，任务文件中不保存 API Key。

目标仓库应同时忽略当前信箱和旧版升级路径。任务正文可能包含私有仓库信息：

```gitignore
/.codex-custom-subagents/
# 从旧版本升级时保留这一行。
/.deepseek-delegations/
```

新批次不会再写入旧目录。如果旧目录还有升级前的未完成任务，使用显式
`--legacy-mailbox` 模式完成该批次；不得改名或合并仍有 worker 使用的信箱。详见
[迁移文档](docs/zh-CN/MIGRATION.md)。

## 跨平台、协议与自定义配置

| 范围 | 已核实支持 | 实际含义 |
| --- | --- | --- |
| Codex Desktop 主机 | macOS、Windows 10/11 | 安装、凭证读取、服务管理、文件锁、任务领取、恢复和诊断都支持这两个平台。目前不宣称支持 Linux 主机。 |
| Codex 侧协议 | Responses | 生成的 Provider 都使用 Codex 当前需要的 Responses 协议。 |
| OpenAI 类上游 | `openai_responses` | Codex 直接请求兼容 OpenAI Responses 的 Provider URL；不代表支持 Chat Completions。 |
| Anthropic 上游 | `anthropic_messages` | 本机 loopback adapter 在 Codex Responses 与 Anthropic Messages 之间转换请求、工具调用、流式事件、响应和 usage。 |
| 自定义配置 | JSON manifest schema v2 | 可定义 Provider URL、远端模型名、协议、凭证引用、上下文/输出声明、agent 名、一个 primary 和有序 fallback。 |

这里的“跨端”指同一套插件可以安装并运行在 macOS 和 Windows Codex Desktop，
不代表两个设备之间自动同步同一个进行中的任务。

内置 preset 只是示例，不是封闭的 Provider 列表。自定义 manifest 可以连接实现
上述任一上游协议的其他服务。manifest 只保存凭证引用；凭证值留在 macOS
Keychain、Windows Credential Manager 或指定环境变量中。

最小结构：

```json
{
  "schema_version": 2,
  "providers": [
    {
      "id": "my_provider",
      "name": "My Provider",
      "base_url": "https://provider.example/api",
      "protocol": "responses",
      "upstream_protocol": "openai_responses",
      "auth": { "type": "env", "variable": "MY_PROVIDER_API_KEY" }
    }
  ],
  "models": [
    {
      "id": "my_model",
      "provider": "my_provider",
      "remote_model": "model-name",
      "agent": "my_model_worker",
      "context_window": 128000,
      "max_context_window": 128000,
      "effective_context_window_percent": 95
    }
  ],
  "selection": { "primary": "my_model", "fallbacks": [] }
}
```

完整字段见[配置文档](docs/zh-CN/CONFIGURATION.md)，协议转换边界见
[Provider 与协议适配](docs/zh-CN/MODEL_ADAPTERS.md)。

## 其他特性

- 每批只使用一个 primary 模型，合格的传输故障可按顺序切换 fallback。
- 支持 macOS Keychain、Windows Credential Manager 和环境变量凭证引用。
- 支持 macOS LaunchAgent 和 Windows Task Scheduler 服务管理。
- 原子任务领取、持久回执、恢复、脱敏诊断和带所有权保护的安全卸载。
- 把 `$deepseek-delegation` 和 `$codex-custom-agents` 安全迁移到 `$codex-custom-subagents`。

## 手动快速安装

要求：已登录 Codex Desktop、Codex `0.146.0` 或更高版本、Python 3.9+、Git，以及目标 Provider 的凭证。

macOS：

```bash
git clone https://github.com/wongchisum/codex-custom-subagents.git
cd codex-custom-subagents
python3 scripts/configure.py --list-model-protocols
python3 scripts/configure.py --primary deepseek-anthropic
```

Windows PowerShell：

```powershell
git clone https://github.com/wongchisum/codex-custom-subagents.git
cd codex-custom-subagents
py -3 scripts\configure.py --list-model-protocols
py -3 scripts\configure.py --primary deepseek-anthropic
```

内置模型/协议组合：

- `deepseek-anthropic`
- `deepseek-openai`
- `gemini-anthropic`
- `claude-anthropic`

fallback 与 primary 分开声明：

```bash
python3 scripts/configure.py \
  --primary claude-anthropic \
  --fallback gemini-anthropic \
  --fallback deepseek-openai
```

如果 `configure.py` 以 exit 3 退出，请在本机运行脚本打印的凭证命令，然后重复相同的 configure 命令。安装或迁移后必须新建 Codex 任务再测试委派，否则旧任务可能返回 `unknown agent_type`。

## 文档

- [文档索引](docs/zh-CN/README.md)
- [安装、升级与卸载](docs/zh-CN/INSTALLATION.md)
- [模型与 fallback 配置](docs/zh-CN/CONFIGURATION.md)
- [Provider 与协议适配](docs/zh-CN/MODEL_ADAPTERS.md)
- [系统架构](docs/zh-CN/ARCHITECTURE.md)
- [迁移](docs/zh-CN/MIGRATION.md)
- [测试](docs/zh-CN/TESTING.md)
- [Windows 真机清单](docs/zh-CN/WINDOWS_TESTING.md)
- [故障排查](docs/zh-CN/TROUBLESHOOTING.md)

`doctor.py` 只验证本机安装状态、凭证引用、Codex 严格配置和 adapter 健康，不调用真实模型，也不检查计费。CI 同时运行 macOS 和 Windows；维护者也在 2026-08-03 确认 PR #1 的完整 Windows 真机清单通过。后续版本仍需重新执行真机清单。

本项目是第三方集成，不是 OpenAI、Anthropic、Google 或 DeepSeek 的官方产品。

## 链接

[Linux Do](https://linux.do/)
