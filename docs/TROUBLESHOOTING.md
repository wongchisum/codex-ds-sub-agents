# 故障排查

## Windows 提示 `Invalid argument/option - 'Create'`

这是旧版本传给 `schtasks` 的 action 缺少 `/` 前缀导致的。支持的调用必须使用 `/Query`、`/Create`、`/Run`、`/End` 和 `/Delete`。

先确认当前分支包含 Issue #2 的修复，再清理失败安装留下的 pending owner：

```powershell
py -3 scripts\uninstall.py --manifest "$env:USERPROFILE\.codex\custom-subagents\manifests\<name>.json" --no-stop-adapters
```

运行 `--dry-run` 后再决定是否执行真实清理。不要手工删除整个 `%USERPROFILE%\.codex`。

## 安装后返回 `unknown agent_type`

Codex 任务在创建时加载 agent 注册表，当前任务不会热加载新安装的 agent。doctor 全部通过也不改变这一点。

处理顺序：

1. 完成 configure 和 doctor。
2. 关闭执行安装的 Codex 任务。
3. 新建 Codex 任务。
4. 在新任务中读取 `subagent-selection.json` 并使用 `$codex-custom-agents`。

不要把 `unknown agent_type` 分类成上游网络、计费或限流错误，也不要触发 fallback。

## Windows 的 `python3` 指向 Store shim

Windows 文档使用 `py -3`。如果没有 Python Launcher，使用实际解释器绝对路径：

```powershell
python -c "import sys; print(sys.executable)"
```

安装器生成的 agent、凭证 helper 和 Task Scheduler 命令必须使用运行安装器的 `sys.executable`，不能写死 `python3` 或 WindowsApps shim。

## 安装失败后存在 pending installation

安装登记表会保留 pending 状态，防止下一次安装掩盖半完成事务。使用原 manifest 运行卸载器清理登记资源；文件摘要不一致或用户修改过的文件会被保留。

## 诊断包

```powershell
py -3 scripts\diagnostics.py --run windows_failure_01 --out diagnostics --format zip --manifest <manifest>
```

诊断包不收集 Credential Manager 值、环境变量值、`config.toml`、Prompt 或任务正文。分享前仍需人工搜索 `sk-`、`Bearer`、`api_key` 和供应商 Key 前缀。
