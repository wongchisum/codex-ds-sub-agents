# 实现原理

## 从配置到 worker

1. `configure.py` 选择预设或接收自定义 manifest，并把经过校验的副本保存到稳定用户目录。
2. `load_manifest()` 严格解析 JSON，拒绝未知协议、非法 URL、内联凭证、重复 ID 和不完整 selection。
3. 配置入口通过 macOS Keychain 或 Windows Credential Manager 检查凭证引用；缺少时在安装前返回 exit 3，不把凭证值写入参数、manifest 或日志。
4. 安装器为每个模型渲染独立 catalog 和 agent TOML，为每个 Provider 渲染 `config.toml` 块。
5. 安装器写入 `subagent-selection.json`，把 model ID 映射到真实 agent、provider、remote model 和上下文声明。
6. `upstream_protocol=anthropic_messages` 时，安装器推导 adapter，并通过 macOS LaunchAgent 或 Windows Task Scheduler 启动带固定 Provider 身份的服务。
7. 凭证齐全后自动运行 doctor。用户新建 Codex 任务，skill 再通过 `delegation_runtime.py begin` 取得唯一的 `active.agent`。

## 任务正文为什么使用文件

自定义 provider 的原生父子消息在部分 Desktop 版本和协议组合中无法稳定携带完整任务正文。任务正文如果只存在于 spawn message，worker 可能收到空任务或只有加密块。

父任务因此先写入完整任务文件，再创建 worker。文件协议头保持 `# DeepSeek task handoff v1`，这是兼容标识，不代表模型必须是 DeepSeek。改头会让旧任务文件被拒绝，所以项目改名不修改协议 v1。

领取时：

```text
验证真实 cwd
  → 锁定任务池
  → 验证 pending 协议头和 task_id
  → 原子 rename 到 claimed/<task_id>--<claim_id>.md
  → 持久化 receipt
  → 返回 task_id、claim_id、path、receipt
```

receipt 记录 `attempt_id`、时间、状态、退出码，以及可空的 parent/worker thread、agent、model、provider。`complete` 和 `fail` 只能更新同一 `task_id + claim_id` 的准确 attempt。

follow-up 必须先更新已经领取的文件，再唤醒同一个 worker。父任务不能靠 glob 猜 claimed 路径。

## fallback 状态机

每个运行在 `runs/<run-id>.json` 保存：

- 当前 model 与 agent
- 已尝试模型集合
- generation，从 1 开始
- 已切换次数与 `max_switches`
- 原始失败分类和最终 outcome

`record-failure` 只有在 transport retry 已耗尽后调用。状态机分类错误并决定 `switched` 或 `blocked`。切换前必须确认旧 worker 已停止，否则两个模型可能同时修改同一任务。

成功结果保留；只恢复未完成的 claim。运行验收后用 `finish --outcome completed` 关闭，无法切换的错误用 `blocked`。

## 安装与回滚

所有文件写入都先在目标目录创建临时文件、`fsync`，再用 `os.replace` 原子替换。已有文件变化时先备份。安装中途启动 adapter 失败，服务管理器停止刚启动的 job，并只删除本次创建且内容仍匹配的 plist。

登记表的资源摘要解决两个问题：

- 用户修改托管文件后，卸载不会覆盖或删除用户内容。
- 多个 manifest 使用相同 skill/adapter 时，移除一个 owner 不会破坏另一个安装。

同名 Provider 配置漂移直接失败。静默复用旧 URL 或旧认证会让 selection 看似切换成功，实际请求仍打到错误上游。

## adapter 服务身份

健康响应包含 `service_id`、adapter 类型和 fingerprint。fingerprint 绑定 Provider ID 与真实上游 URL。安装器和 doctor 不只检查端口是否返回 200；旧服务或占用端口的其他进程无法冒充正确 adapter。

审计 JSONL 只记录 request ID、模型、请求与实际 token 上限、状态、错误类别、耗时和上游 usage。Prompt 与凭证不进入审计。

## 工具边界

外部模型 worker 保留仓库工作需要的本地工具，例如 `exec_command`、`write_stdin`、`update_plan` 和 freeform `apply_patch`。以下能力不会下发：

- 创建 sub-agent：避免递归委派和失控并发。
- `request_user_input`：worker 必须把阻塞返回父任务。
- 内置 web search 与图像工具：当前没有跨供应商协议映射。

这不是模型本身缺少工具，而是父任务对子任务权限的明确限制。

## 上下文核验

安装时的 `context_window` 和 `max_context_window` 只生成 catalog 元数据。真实 Desktop session 可能被客户端、模型目录或服务端限制为更小窗口。

```bash
python3 scripts/session_audit.py \
  --rollout ~/.codex/sessions/YYYY/MM/DD/rollout-....jsonl \
  --selection ~/.codex/models/subagent-selection.json
```

审计输出同时给出 declared 和 `task_started.model_context_window`，并标记首轮非空结果、follow-up、最后输出 token 与原生错误。当前一次 1M catalog 测试的实际窗口是 `258400`，因此不能声称已经获得 1M 原生 sub-agent 上下文。
