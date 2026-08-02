# Codex Custom Subagents

[English](README_EN.md) · [安装](docs/INSTALLATION.md) · [架构](docs/ARCHITECTURE.md) · [模型适配](docs/MODEL_ADAPTERS.md) · [实现原理](docs/IMPLEMENTATION.md)

`codex-custom-subagents` 让 Codex Desktop 使用自定义供应商、模型地址和协议创建 sub-agent。配置可以列出多个候选模型，但每一批 sub-agent 只使用一个模型；当前模型发生符合条件的传输故障后，父任务可按配置切换到下一个 fallback。

项目以 Codex 插件形式提供 `deepseek-delegation` skill，并附带安装器、模型目录生成器、协议适配器，以及 macOS LaunchAgent / Windows Task Scheduler 服务管理工具。

![Codex Desktop 中的自定义子智能体任务](assets/codex-custom-subagents.png)

截图来自 DeepSeek worker 的本地测试。项目也支持 Claude、Gemini、DeepSeek Anthropic 端点，以及其他兼容 Anthropic Messages 的网关。

## 核心能力

- 自定义 `base_url`、远端模型名、认证引用、协议、上下文声明和输出上限。
- 一个 primary 加有序 fallbacks；同一运行批次不混用模型。
- 原生 Codex Responses provider，或通过本机适配器接入 Anthropic Messages。
- macOS Keychain、Windows Credential Manager、Bearer 环境变量和自定义 Header 环境变量认证；manifest 禁止内联 Key。
- 原子任务信箱，解决部分自定义 provider 无法可靠收到原生任务正文的问题。
- 安装所有权登记、内容摘要校验、可预览卸载和旧版 DeepSeek 安装兼容。

## 快速开始

要求：macOS 或 Windows、已登录的 Codex Desktop、Python 3.9+、`git`，以及目标模型凭证。Windows 代码路径已有模拟测试，真机验收步骤见 [Windows 测试清单](docs/WINDOWS_TESTING.md)。

```bash
git clone https://github.com/wongchisum/codex-custom-subagents.git
cd codex-custom-subagents

# 查看协议；Provider 按协议配置，不按模型类型配置
python3 scripts/configure.py --list-protocols

# 兼容示例：安装 DeepSeek V4 Flash，并自动运行 doctor
python3 scripts/configure.py --profile deepseek-anthropic
```

`configure.py` 会把选中的 manifest 保存到
`~/.codex/custom-subagents/manifests/`，检查凭证引用，并在凭证齐全后依次运行安装和
doctor。`--profile` 仅保留为示例和旧 Prompt 兼容入口；新配置使用 schema v2 manifest，
Provider 只声明 `openai_responses` 或 `anthropic_messages`。首次运行如果缺少凭证，脚本会
在安装前以 exit 3 停止并打印当前操作系统的交互式凭证命令。

```bash
/usr/bin/security add-generic-password -U -a codex -s deepseek-api-key -w
```

macOS 的 `-w` 后不附带值；Windows 使用 `credential_store.py set`，两者都交互读取密钥，
不会把值写进命令历史。保存后重新运行同一条 `configure.py`
命令即可继续。安装完成后必须新建 Codex 任务；已经打开的任务不会热加载新 agent
类型。

其他配置：

```bash
# Claude primary，Gemini fallback
python3 scripts/configure.py --profile claude-gemini

# 仅 Gemini
python3 scripts/configure.py --profile gemini-anthropic

# 第一版 DeepSeek 固定配置，继续兼容
python3 scripts/configure.py --profile legacy-deepseek

# 安装用户自己生成的 manifest
python3 scripts/configure.py \
  --manifest config/my-team.local.json \
  --name my-team
```

完整步骤、升级和卸载规则见 [安装文档](docs/INSTALLATION.md)。

## 让 Codex 帮你安装和配置

把下面整段 Prompt 发送给 Codex。不要在 Prompt 中补充 API Key；配置脚本发现凭证
缺失后，Codex 必须停下来让你在本机终端交互输入。

```text
请为我安装并配置 Codex Custom Subagents：
https://github.com/wongchisum/codex-custom-subagents

执行要求：
1. 检查操作系统、python3 --version、codex --version 和目标目录的 Git 状态。
   如果仓库不存在，克隆到有写权限的开发目录；如果已经存在，保留所有未提交修改，
   不执行 reset、checkout 或清理命令。
2. 阅读 README.md、docs/INSTALLATION.md、scripts/configure.py 和
   config/model-providers.example.json。先向我确认 primary、fallback 顺序和配置来源。
3. 如果我选择内置配置，只能使用下列命令之一：
   python3 scripts/configure.py --profile deepseek-anthropic
   python3 scripts/configure.py --profile gemini-anthropic
   python3 scripts/configure.py --profile claude-gemini
   python3 scripts/configure.py --profile legacy-deepseek
4. 如果我选择自定义模型，先按 Provider 收集：base_url、upstream_protocol
   （只能是 openai_responses 或 anthropic_messages）和凭证引用；再收集每个模型的：remote_model、
   Keychain service 名、上下文声明、输出上限，以及 primary/fallback 顺序。
   不要索取 API Key。基于示例生成 config/codex-user.local.json，文件中只能保存
   凭证引用，不能保存 key、token 或 secret，然后运行：
   python3 scripts/configure.py --manifest config/codex-user.local.json --name codex-user
5. 如果 configure.py 以 exit 3 停止，原样展示它输出的交互式凭证命令并暂停。
   让我在本机终端输入凭证；不要代替我读取、打印或转存 Keychain 内容。
6. 我确认凭证已保存后，重新运行完全相同的 configure.py 命令。不要单独手工修改
   ~/.codex/config.toml、agent TOML、model catalog 或 LaunchAgent。
7. 只有 doctor 全部 PASS 后才报告本地安装完成，同时明确 doctor 没有调用真实模型。
   提示我新建 Codex 任务，再提供一个使用 $deepseek-delegation 的最小测试 Prompt。
8. 不提交、不推送、不修改 Git remote，也不把 API Key 写入命令参数、文件、日志或对话。
```

如果用户已经给出 primary、fallback、URL 和模型名，Codex 可以直接生成自定义 manifest；
仍然不能代替用户输入凭证。

## 使用

目标项目的 `.gitignore` 应包含：

```gitignore
/.deepseek-delegations/
```

示例 Prompt：

```text
使用 $deepseek-delegation，把代码审查和测试分析拆成两个独立任务。
读取当前 subagent-selection.json，整批使用其中 active.agent，
以 fork_turns: "none" 同时创建 worker。主智能体负责验收结果；
不要在同一批次混用 primary 与 fallback。
```

`deepseek-delegation` 是为旧版兼容保留的 skill ID，不代表只能调用 DeepSeek。manifest 选中的 agent 可以绑定 Claude、Gemini、DeepSeek 或其他受支持模型。

## 文档

- [安装、升级与卸载](docs/INSTALLATION.md)
- [系统架构](docs/ARCHITECTURE.md)
- [模型、Provider 与协议适配](docs/MODEL_ADAPTERS.md)
- [任务信箱、fallback 与安全实现](docs/IMPLEMENTATION.md)
- [从旧版名称与固定 DeepSeek 配置迁移](docs/MIGRATION.md)
- [测试范围与命令](docs/TESTING.md)
- [Windows 真机兼容测试与日志采集](docs/WINDOWS_TESTING.md)
- [当前实测报告](TEST_REPORT.md)

## 已验证边界

- Codex 最低客户端声明为 `0.146.0`；当前完整测试快照使用 `0.146.0-alpha.9.2`。
- 真实调用已覆盖 DeepSeek V4 Flash、Claude Code 兼容端点和 Gemini Anthropic 兼容端点；详见测试报告。
- catalog 可声明 1M 上下文，但一次原生 Desktop sub-agent 实测的 `model_context_window` 为 `258400`。声明值不等于运行时实际值。
- `doctor.py` 只检查安装、凭证条目和 adapter 健康；它不会发送真实模型请求，也不验证余额。
- 当前 Codex 运行时 provider 必须使用 `responses`。Anthropic Messages 通过本机 adapter 转换。
- Windows 目前完成了平台分支和模拟自动化测试；发布前仍需按清单完成 Windows 10/11 真机验证。

本项目是第三方集成，不是 OpenAI、Anthropic、Google 或 DeepSeek 的官方产品。
