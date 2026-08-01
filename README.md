# Codex DeepSeek Subagents

[English](README_EN.md) · [架构说明](ARCHITECTURE.md)

这个项目让 Codex Desktop 把 `deepseek-v4-flash` 用作子智能体模型，并允许父任务同时下发多个相互独立的工作项。

> 这是第三方集成，不是 OpenAI 或 DeepSeek 发布的 Codex 插件。仓库当前没有 `.codex-plugin/plugin.json`；安装脚本写入的是 Codex 的 agent、model catalog、model provider 和 skill 配置。

![Codex Desktop 中的 DeepSeek 子智能体任务](assets/codex-deepseek-subagents.png)

上图来自一次本地测试。右侧是 Codex Desktop 的子智能体任务列表，其中多个 DeepSeek worker 并行执行审查、修复和协议验证。任务名称、数量和界面会随实际 Prompt 与 Codex 版本变化。

## 工作方式

在当前测试环境中，Codex 创建自定义 provider 子智能体时，传给 DeepSeek 的原生父子消息可能缺少完整任务正文或任务名。本项目用工作区文件传递任务正文，同时保留 Codex Desktop 的子任务列表、工具执行和结果回传：

```text
父智能体拆分独立任务
  → 写入 .deepseek-delegations/pending/
  → 创建一个或多个 deepseek_worker
  → 每个 worker 原子领取一个任务到 claimed/
  → worker 执行任务并返回 task_id、claim_id、路径和 receipt
  → 父智能体检查文件与测试结果
```

领取脚本用同一文件系统内的原子 rename 避免两个 worker 执行同一任务。receipt 用于定位已经领取但未成功回传结果的任务；`recover` 不会自行判断 worker 是否已经停止，恢复前仍需父智能体确认。

## 已验证范围

- macOS
- Codex Desktop 与 Codex CLI `0.146.0-alpha.3.1`
- Python 3.9 和 3.13；CI 使用 Python 3.11
- DeepSeek 模型 `deepseek-v4-flash`
- DeepSeek Responses API base URL `https://api.deepseek.com`
- macOS Keychain 中的 `deepseek-api-key`

`models/deepseek-v4-flash.json` 当前声明 `minimal_client_version` 为 `0.146.0`。这只是本项目的客户端版本门槛，不代表已经逐个验证所有更早或更晚的 Codex 版本。

DeepSeek 官方文档目前列出 `deepseek-v4-flash`，模型版本为 `DeepSeek-V4-Flash-0731`，并说明 Responses API 当前支持该模型：

- [DeepSeek API 快速开始](https://api-docs.deepseek.com/zh-cn/)
- [DeepSeek Responses API 兼容性](https://api-docs.deepseek.com/zh-cn/guides/responses_api/)

## 安装

### 前置条件

- macOS
- 已安装并登录 Codex Desktop
- `git` 与 `python3`
- 可用的 DeepSeek API Key

### 克隆并安装

```bash
git clone https://github.com/wongchisum/codex-ds-sub-agents.git
cd codex-ds-sub-agents
python3 scripts/install.py
```

`install.py` 会：

- 安装 `deepseek_worker` 到 `~/.codex/agents/`
- 安装模型目录到 `~/.codex/models/`
- 安装 `deepseek-delegation` skill 到 `~/.codex/skills/`
- 在缺少配置时向 `~/.codex/config.toml` 追加 DeepSeek provider
- 覆盖本项目管理的已修改文件前创建带时间戳的备份

安装器不会改动仓库的 `worktree/`。升级 skill 时，它只清理旧安装清单中记录、已从新版本移除且内容未被用户修改的文件。

### 保存 API Key

不要把 Key 写进 README、Prompt、Git 配置或任务文件。在终端运行：

```bash
/usr/bin/security add-generic-password -U -a codex -s deepseek-api-key -w
```

`-w` 放在命令末尾且不带值时，macOS `security` 会交互式读取密码，Key 不会出现在命令历史中。

然后检查安装：

```bash
python3 scripts/doctor.py
```

`doctor.py` 检查安装文件、Keychain 条目和 Codex 严格配置加载。它不会向 DeepSeek 发送请求，也不会验证余额、网络连通性或实际模型调用。

安装后新建一个 Codex 任务。若新任务仍未发现 `deepseek-delegation` 或继续使用旧 agent 指令，再重启 Codex Desktop。是否需要重启取决于当前 Desktop 进程是否已经缓存这些文件。

## 让 Codex 帮你安装

把下面整段 Prompt 发送给 Codex。不要把 DeepSeek API Key 粘贴进对话；Codex 应在需要写入 Keychain 时停下来，让你在本机终端输入。

```text
请安装并验证 Codex DeepSeek Subagents，仓库地址：
https://github.com/wongchisum/codex-ds-sub-agents

要求：
1. 先检查当前系统是否为 macOS，并运行 codex --version 与 python3 --version；记录真实输出，不猜测兼容性。
2. 如果本地没有仓库，克隆到我有写权限的开发目录；如果已经存在，先检查工作区状态，不覆盖未提交修改。
3. 阅读 README.md、scripts/install.py、config/deepseek-provider.toml、agents/deepseek-worker.toml.template 和 models/deepseek-v4-flash.json，确认安装目标和将要修改的 ~/.codex 文件。
4. 运行 python3 scripts/install.py。不要把 API Key 写入命令、文件、日志或对话。
5. 安装脚本完成后，停下来提示我在本机终端运行：
   /usr/bin/security add-generic-password -U -a codex -s deepseek-api-key -w
6. 我确认 Key 已保存后，再运行 python3 scripts/doctor.py。
7. 报告实际修改的文件、备份文件、doctor 结果和未验证事项。doctor 不会调用真实 API，所以不要把 doctor PASS 描述为端到端成功。
8. 不要提交、推送或修改仓库远端。不要读取或打印 Keychain 中的 Key。
```

如果 Codex 无法写入 `~/.codex`，它应请求一次范围明确的文件写入授权，而不是改用其他目录并声称安装成功。

## 使用

先在目标项目的 `.gitignore` 中加入：

```gitignore
/.deepseek-delegations/
```

然后在 Codex 中使用：

```text
使用 $deepseek-delegation，把以下两个独立任务放入同一个任务池，并同时创建两个 deepseek_worker，全部使用 fork_turns: "none"。主智能体只负责拆分、等待和最终验收。

任务一，ID 为 analyze_auth：
分析 src/auth 的调用关系和错误边界，不修改代码，报告文件与行号证据。

任务二，ID 为 test_users：
运行用户模块测试，定位失败根因；只允许修改 tests/users，完成后重新运行相关测试。

验收要求：
1. 每个 worker 返回不同的 task_id、claim_id、claimed path 和 receipt。
2. 每个 worker 只执行自己领取的任务。
3. 主智能体检查真实文件与测试输出，并报告 worker 和任务的对应关系。
```

任务正文应写明目标、文件范围、是否允许修改、输出格式和验证命令。不要并行执行依赖同一批未提交修改的任务。并发数量受当前 Codex agent slots 限制，任务较多时会分批执行。

更多示例见 [examples/parallel-prompt.md](examples/parallel-prompt.md)。

## 本地测试目录

仓库的 `worktree/` 用于隔离子智能体测试，每个任务一个目录：

```text
worktree/
  task-001/
  task-002/
```

`worktree/` 和仓库根目录的 `.deepseek-delegations/` 已被 Git 忽略，不应提交。目标项目也需要单独忽略自己的 `.deepseek-delegations/`。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 scripts/doctor.py
```

端到端验证必须在 Codex Desktop 中实际创建至少一个 `deepseek_worker`，确认它能领取任务、调用所需工具并回传结果。单独运行 `doctor.py` 不能证明 API 调用成功。

## 卸载

先预览：

```bash
python3 scripts/uninstall.py --dry-run
```

确认后卸载：

```bash
python3 scripts/uninstall.py
```

卸载器只删除与当前项目安装内容完全一致的 agent、模型和 skill 文件。用户修改过的文件、未知文件和符号链接会保留。只有当 DeepSeek provider 块与项目模板完全一致并位于 `config.toml` 末尾时，卸载器才会移除它；修改配置前会创建备份。

## 限制与安全边界

- 当前只实现 macOS Keychain 认证；Linux 和 Windows 尚未支持。
- 任务正文保存在工作区明文文件中，不得包含密钥、访问令牌或隐私数据。
- worker 请求会发送到 DeepSeek API，而不是 OpenAI API。使用前应确认代码和数据允许发送给该服务。
- 模型目录把输入声明为文本；不要依赖图片输入。
- DeepSeek Responses API 只兼容部分 OpenAI Responses API 字段和工具。具体支持范围以 [DeepSeek 官方兼容性说明](https://api-docs.deepseek.com/zh-cn/guides/responses_api/) 为准。
- 自定义 agent、model catalog 和 provider 配置可能随 Codex 版本变化。升级 Codex 后应重新运行测试和真实 worker 验证。
- worker 异常退出后不会自动重排任务。父智能体确认 worker 已停止后，才能用 `recover` 恢复。
- 仓库尚未选择开源许可证。在许可证加入前，默认版权规则仍然适用。

协议与恢复流程见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## Links
[Linux Do](https://linux.do/)
