# 名称迁移与兼容

## 本次改名范围

项目和插件的公开名称从 `Codex DeepSeek Subagents` 改为 `Codex Custom Subagents`，机器名称为 `codex-custom-subagents`。新的仓库地址按以下名称记录：

```text
https://github.com/wongchisum/codex-custom-subagents
```

本地工作区目录不会由安装器自动移动，GitHub 仓库也不会由代码自动改名。完成远端仓库改名后，旧 clone 可以手工更新 remote。

## 保留的兼容标识

以下标识暂不改名：

| 标识 | 保留原因 |
| --- | --- |
| `deepseek-delegation` | 已发布的 skill ID、旧 Prompt 和已安装路径依赖它 |
| `.deepseek-delegations/` | 已有任务池和恢复记录依赖该目录 |
| `# DeepSeek task handoff v1` | 任务文件协议版本；改名会拒绝旧 pending/claimed 文件 |
| `.codex-deepseek-manifest.json` | 第一版 skill 卸载摘要 |
| `deepseek_worker` | 第一版 agent 类型，旧 Desktop 任务会引用它 |
| `deepseek-api-key` | 用户现有 Keychain service |
| `[model_providers.deepseek]` | 第一版固定 provider 配置 |

这些名称是兼容 API，不是新的产品定位。manifest 生成的 agent 和 provider 仍按用户配置命名。

## 从第一版升级

第一版固定 DeepSeek 安装可以与 manifest 安装共存：

- 固定 catalog 是 `deepseek-v4-flash.json`。
- manifest catalog 是 `<provider-id>--<model-id>.json`。
- `subagent-selection.json` 存在时，skill 使用 selection 选择 agent。
- selection 不存在时，skill 回退到 `deepseek_worker`。

推荐顺序：

```bash
# 1. 在旧仓库目录检查当前安装
python3 scripts/doctor.py --skip-keychain

# 2. 如果要移动仓库目录，先用原 manifest 预览并卸载 manifest 安装
python3 scripts/uninstall.py --manifest /absolute/path/to/original.json --dry-run
python3 scripts/uninstall.py --manifest /absolute/path/to/original.json

# 3. 从新目录通过稳定配置入口重新安装
python3 scripts/configure.py --profile claude-gemini
```

manifest installation ID 包含 manifest 绝对路径。直接移动目录后重装会产生新 ID，旧 owner 记录不会自动转移。
新配置入口把 manifest 复制到 `~/.codex/custom-subagents/manifests/`，后续移动仓库不会再
改变 installation ID。

## 已有任务与 Desktop 缓存

安装或新增 agent 后必须新建 Codex 任务。旧任务缓存的 agent 注册表不会热更新，可能返回 `unknown agent_type`。这不是上游模型错误，请勿触发 fallback。

已有 `.deepseek-delegations` 任务池无需改名。迁移前检查 `pending/` 与 `claimed/`，不要把未知任务带进新批次；确认没有 worker 运行后再执行恢复操作。

## Git remote

项目代码不会擅自修改外部 Git 状态。远端仓库实际改名后，用户可执行：

```bash
git remote set-url origin https://github.com/wongchisum/codex-custom-subagents.git
git remote -v
```

在 GitHub 尚未创建或重命名仓库前不要执行，否则 pull/push 会指向不存在的地址。
