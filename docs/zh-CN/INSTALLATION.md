[English](../INSTALLATION.md) · 简体中文

# 安装、升级与卸载

## 安装结果

安装器把运行文件写入 `CODEX_HOME`，默认是 `~/.codex`：

```text
~/.codex/
├── agents/                         # worker 定义
├── models/                         # model catalog 与 subagent-selection.json
├── skills/codex-custom-subagents/      # 当前 Skill：$codex-custom-subagents
├── adapters/                       # Anthropic Messages adapter
├── logs/adapters/                  # 运行日志与不含正文的审计日志
├── logs/custom-subagents/          # configure 分阶段日志，输出已脱敏
├── helpers/credential_store.py     # macOS/Windows 凭证读取边界
├── config.toml                     # model_providers 配置
└── .codex-subagent-installations.json
```

使用 adapter 的 manifest 会在 macOS 创建 LaunchAgent，在 Windows 创建当前用户的 Task Scheduler 任务。服务只监听 loopback，默认随登录启动。

## 安装前置条件

- macOS 或 Windows。Windows 真机发布验收尚未完成，先阅读 [Windows 测试清单](WINDOWS_TESTING.md)。
- 已安装并登录 Codex Desktop。
- Python 3.9+ 与 `git`。
- 目标供应商的有效凭证。

先克隆新名称的仓库：

```bash
git clone https://github.com/wongchisum/codex-custom-subagents.git
cd codex-custom-subagents
```

## 推荐：统一配置入口

先查看支持的 Provider 协议：

```bash
python3 scripts/configure.py --list-model-protocols
```

新配置应使用 schema v2 manifest。下列 profile 是兼容示例：

```bash
python3 scripts/configure.py --primary deepseek-anthropic
python3 scripts/configure.py --primary gemini-anthropic
python3 scripts/configure.py --primary claude-anthropic --fallback gemini-anthropic
```

不传参数并在交互式终端运行时，脚本会显示数字菜单。Codex 或 CI 等非交互环境必须
显式传入 `--primary`（可重复追加 `--fallback`）或 `--manifest`，避免猜测用户要安装哪个模型。`--profile` 只供旧脚本兼容。

`configure.py` 完成以下工作：

1. 严格校验选中的 manifest。
2. 保存到 `~/.codex/custom-subagents/manifests/<name>.json`；POSIX 使用 0600。
3. 检查 manifest 引用的 macOS Keychain、Windows Credential Manager 或环境变量是否存在。
4. 凭证齐全后调用 `install.py` 安装 provider、agent、catalog、skill 和 adapter 服务。
5. 凭证齐全时自动调用 `doctor.py`。

凭证缺失时会在安装运行组件前返回 exit 3，并打印用户应在本机执行的交互式命令。
保存凭证后重复同一条 `configure.py` 命令即可；安装过程是幂等的。

### 内置配置

这些名称不是 Provider 抽象，只是第一版命令和测试配置的兼容别名。

| profile | 当前 primary | 说明 |
| --- | --- | --- |
| `deepseek-anthropic` | DeepSeek V4 Flash | Anthropic Messages 端点，catalog 声明 1M |
| `gemini-anthropic` | Gemini 3.5 Flash | 单模型配置，输出上限 4096 |
| `claude-gemini` | Claude Opus 4.6 | Gemini 3.5 Flash 为 fallback |
| `legacy-deepseek` | DeepSeek V4 Flash | 第一版固定安装，不使用 manifest |

### 自定义配置

自定义 manifest 推荐使用 Git 忽略的 `config/*.local.json`：

```bash
python3 scripts/configure.py \
  --manifest config/my-team.local.json \
  --name my-team
```

`--name` 决定稳定副本的文件名和安装所有权路径。移动仓库或删除临时源文件后，卸载仍可
使用 `~/.codex/custom-subagents/manifests/my-team.json`。同名稳定副本已经存在但内容不同
时，配置脚本会拒绝覆盖，避免安装失败后破坏旧配置的卸载依据；请改用新的 `--name`，
或者先卸载原配置。

### 安装结果

底层安装器会生成：

- `~/.codex/agents/<agent>.toml`
- `~/.codex/models/<provider-id>--<model-id>.json`
- `~/.codex/models/subagent-selection.json`
- `~/.codex/config.toml` 中的 provider 块
- adapter 脚本、LaunchAgent 和资源所有权记录

只想检查生成文件、不启动 adapter 时使用 `--no-start-adapters`。这种安装不能创建依赖 adapter 的真实 worker，直到服务启动并通过健康检查。

需要调试底层安装器时仍可直接运行：

```bash
python3 scripts/install.py --manifest config/model-providers.example.json
python3 scripts/doctor.py --manifest config/model-providers.example.json
```

这些命令不会编排凭证检查步骤，普通用户应使用 `configure.py`。

## 兼容安装：固定 DeepSeek 配置

统一入口：

```bash
python3 scripts/configure.py --profile legacy-deepseek
```

直接调用底层安装器时，不传 `--manifest` 会安装第一版固定配置：

```bash
python3 scripts/install.py
python3 scripts/doctor.py
```

该流程继续安装 `deepseek_worker`、`deepseek-v4-flash.json` 和 `[model_providers.deepseek]`。它用于旧 Prompt 和旧安装升级，不是新配置的推荐入口。

## 保存凭证

manifest 只允许凭证引用，不允许 `api_key`、`key`、`secret`、`token` 或 `value` 内联字段。Keychain 示例：

```bash
/usr/bin/security add-generic-password -U -a codex -s deepseek-api-key -w
```

命令末尾的 `-w` 不附带值时，Key 不会进入 shell history。不同 manifest 可复用同一个 Keychain service，也可以分别声明 service。

Windows 使用交互式 Credential Manager helper：

```powershell
py -3 scripts\credential_store.py set --account codex --service deepseek-api-key
```

不要把 Key 追加在命令末尾。安装后的 Provider 通过 `helpers/credential_store.py get` 读取；日志只记录 service/account，不记录值。

还支持两种环境变量引用：

- `auth.type = "env"`：使用指定环境变量作为 Bearer token。
- `auth.type = "env_header"`：把环境变量写入指定认证 Header。

LaunchAgent 不一定继承交互式 shell 环境。生产式本地安装优先使用 Keychain。

## 检查安装

```bash
python3 scripts/doctor.py \
  --manifest ~/.codex/custom-subagents/manifests/deepseek-anthropic.json
```

doctor 检查 selection、provider、agent、catalog、凭证条目、Codex 严格配置加载，以及 adapter `/health` 返回的 `service_id` 与 fingerprint。以下选项只适合离线诊断：

```bash
python3 scripts/doctor.py \
  --manifest ~/.codex/custom-subagents/manifests/claude-gemini.json \
  --skip-keychain
python3 scripts/doctor.py \
  --manifest ~/.codex/custom-subagents/manifests/claude-gemini.json \
  --skip-adapter-health
```

doctor 不发送模型请求，不验证网络、余额、模型权限或真实工具循环。安装后新建 Codex 任务，再做端到端测试。

## 导出诊断包

`configure.py` 会输出本次 JSONL 日志路径。需要跨环境排查时执行：

```bash
python3 scripts/diagnostics.py --run windows_case_01 --out diagnostics --format zip \
  --manifest ~/.codex/custom-subagents/manifests/my-team.json
```

诊断包包含平台版本、脱敏 manifest、selection/安装登记摘要、adapter 健康状态、有限长度的 adapter/configure 日志和任务 receipt 摘要。它明确排除凭证值、环境变量值、`config.toml`、Prompt 与任务正文。分享前仍应人工检查压缩包。

## 升级规则

- 写入文件使用同目录临时文件和原子替换。
- 替换已变化的托管文件前创建带 UTC 时间戳的 `.bak.*` 备份。
- 同名 Provider 已存在但 URL、认证、协议或 adapter 配置不同，安装会中止，不会静默覆盖。
- `~/.codex/.codex-subagent-installations.json` 记录资源摘要、owner、安装顺序和 selection；共享文件可以由多个安装共同持有。
- skill 内的 `.codex-deepseek-manifest.json` 是旧版兼容清单，名称暂不改变。

如果仓库目录从旧名称移动到新名称，manifest 绝对路径会产生新的 installation ID。先在旧目录用原 manifest 卸载，再从新目录安装；不要直接制造两条互不认识的所有权记录。详见 [迁移文档](MIGRATION.md)。

通过 `configure.py` 安装的新配置使用 `~/.codex/custom-subagents/manifests/` 中的稳定
路径，不再随仓库目录移动。上述路径迁移规则只影响直接把仓库内 manifest 传给旧版
`install.py` 的安装。

## 卸载

先预览：

```bash
python3 scripts/uninstall.py \
  --manifest ~/.codex/custom-subagents/manifests/deepseek-anthropic.json \
  --dry-run
```

确认后执行：

```bash
python3 scripts/uninstall.py \
  --manifest ~/.codex/custom-subagents/manifests/deepseek-anthropic.json
```

卸载器只删除摘要与安装记录一致、没有其他 owner 的资源。用户修改过的文件、符号链接、未知文件和仍被其他安装共享的资源都会保留。卸载当前 selection 后，会恢复安装登记表中上一个仍有效的 selection。

原 manifest 已丢失时，可用原路径配合 `--no-stop-adapters` 清理登记资源；停止 LaunchAgent 仍需要 manifest 来确认服务身份。固定 DeepSeek 配置的卸载不传 `--manifest`。
