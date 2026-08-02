# Windows 真机兼容测试

当前仓库已实现 Windows Credential Manager、Task Scheduler 服务、跨平台文件锁和动态 Python 路径。自动化测试在 macOS 上模拟 Windows 分支，不能替代 Windows 10/11 真机验收。

## 环境记录

在 PowerShell 中保存以下输出，禁止保存凭证值：

```powershell
$PSVersionTable.PSVersion
python --version
python -c "import platform,sys; print(platform.platform()); print(sys.executable)"
codex --version
git rev-parse HEAD
```

同时记录 Codex Desktop 版本、Windows build、manifest 文件名和测试 Provider 的 `upstream_protocol`。

## 安装与凭证

```powershell
python scripts/configure.py --list-protocols
python scripts/configure.py --manifest config\my-team.local.json --name my-team
```

首次执行应以 exit 3 停止并打印 `credential_store.py set` 命令。执行该交互式命令后，重复完全相同的 configure 命令。检查：

- 命令行、PowerShell history、manifest 和 configure 日志都没有 Key。
- `%USERPROFILE%\.codex\helpers\credential_store.py` 已安装。
- `config.toml` 的认证命令使用当前 Python 和 helper，不含 `/usr/bin/security`。
- Anthropic Messages Provider 创建当前用户 Task Scheduler 任务，不要求管理员权限。
- adapter `/health`、doctor 和 Codex 严格配置检查通过。

## 自动化测试

```powershell
python -m unittest discover -s tests -v
python -m unittest tests.test_platform_runtime tests.test_platform_lock tests.test_adapter_service -v
python -m unittest tests.test_credential_store tests.test_configure tests.test_diagnostics -v
```

记录通过、失败、跳过数量和完整原始错误。不要为了得到全绿结果删除有效测试。

## Desktop 端到端

每次安装或切换 agent 后新建 Codex 任务：

1. 读取 `subagent-selection.json`，核对 primary、agent、provider 和 remote model。
2. 用 `$deepseek-delegation` 创建一个只读任务，确认 atomic claim 回执含正确 agent/model/provider。
3. 再执行一个会修改临时测试文件并运行测试的工具循环，父任务验收真实产物。
4. 让 worker 完成 claim，确认 receipt 为 `completed`、exit code 为 0。
5. 重启 Codex Desktop 或重新登录 Windows，确认 Task Scheduler 能重新启动 adapter。
6. 制造可控的 `timeout` 或 `service_unavailable`，验证整批切换到 fallback；认证失败不得切换。
7. 卸载前运行 `--dry-run`，再卸载，确认只删除登记且摘要一致的文件与任务。

## 诊断包

出现失败后不要清理现场，立即执行：

```powershell
python scripts/diagnostics.py --run windows_case_01 --out diagnostics --format zip --manifest "$env:USERPROFILE\.codex\custom-subagents\manifests\my-team.json"
```

诊断包应包含：

- Windows/Python/Codex 版本；
- 脱敏 manifest、selection 和安装登记摘要；
- adapter 健康结果、audit/stdout/stderr 尾部；
- 最近 5 次 configure 分阶段日志；
- mailbox receipt 与 run-state 摘要。

工具不会收集 `config.toml`、环境变量值、Credential Manager 内容、Prompt 或任务正文。发送诊断包前仍需解压并搜索 `sk-`、`Bearer`、`api_key` 和供应商密钥前缀；发现明文时不要上传。

## 验收结论

只有安装、重启、真实 worker 工具循环、fallback、卸载和诊断包检查全部完成，才能把 Windows 标记为“真机验证通过”。在此之前应写“Windows 实现完成，真机未验证”，不能写“支持已验证”。
