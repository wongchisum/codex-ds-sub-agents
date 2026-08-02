# 测试与验收

## 自动化测试

```bash
python3 -m unittest discover -s tests -v
python3 tests/test_release_assets.py
python3 -m py_compile scripts/*.py skills/deepseek-delegation/scripts/*.py
```

测试覆盖统一配置入口、manifest 校验、selection/fallback、安装所有权、卸载保护、协议转换、SSE、adapter 服务、任务领取与恢复、session audit 和发布文件。配置入口测试会核对稳定 manifest 路径、0600 权限、共享凭证去重、安装失败传播，以及凭证缺失时不会提前安装或运行 doctor。Python 3.9 缺少 `tomllib` 时会跳过依赖 TOML 解析的发布测试；CI 使用 Python 3.11。

## 安装检查

```bash
python3 scripts/doctor.py \
  --manifest ~/.codex/custom-subagents/manifests/deepseek-anthropic.json
```

PASS 只证明本地配置和服务身份正确。它不证明：

- 上游网络可达
- 账户有余额或模型权限
- 模型名被网关接受
- worker 能完成工具循环
- fallback 能在真实错误后切换

## Desktop 端到端验收

每个新 agent 安装后都新建任务，并完成：

1. 父任务读取 `subagent-selection.json`，报告 primary、agent 和 provider。
2. 写入唯一 task ID 的 pending 文件。
3. 创建 selection 指定的 worker，使用 `fork_turns: "none"`。
4. worker 返回 `task_id`、`claim_id`、claimed path 和 receipt。
5. worker 读取一个真实项目文件，运行一条只读命令，再完成一个小型工具循环。
6. worker 调用 `complete`，父任务核对 receipt 为 `completed` 且 exit code 为 0。
7. 父任务检查真实产物或测试结果，不只接受模型的文字声明。
8. 用 `session_audit.py` 核对 agent、model、provider 和实际上下文窗口。

## fallback 验收

fallback 测试必须使用可控制且符合切换规则的失败，不要用认证错误或错误模型名冒充网络故障：

1. primary 成功创建 worker，确认整批只使用 primary。
2. 让 primary 出现已耗尽的 `network`、`timeout`、`rate_limit`、`billing` 或 `service_unavailable`。
3. 确认旧 worker 全部停止。
4. `record-failure` 返回 `status: switched`，generation 增加且 active agent 变为下一个候选。
5. 只恢复未完成 claim，新批次全部使用 fallback agent。
6. 成功后关闭 run，确认不会回到已尝试模型。

再分别验证 `auth`、`invalid_request`、`model_not_found` 和 `task_failure` 返回 blocked，不发生切换。

## 当前证据与未验证项

当前详细结果保存在 [`../TEST_REPORT.md`](../TEST_REPORT.md)。仍需把以下事项视为未验证，除非产生新的真实记录：

- 1M 原生 Desktop sub-agent 的有效上下文；现有一次记录为 `258400`。
- 接近 1M token 的高成本边界请求。
- Claude → Gemini 的真实端到端 fallback。
- 真实计费、限流与网络故障分类。
- 多轮 compaction 后的长任务稳定性。

测试报告应记录日期、操作系统、Python/Codex 版本、manifest、agent、remote model、任务 ID、receipt、原生错误、实际窗口和诊断包文件名，不记录 API Key 或 Prompt 中的秘密。Windows 步骤见 [WINDOWS_TESTING.md](WINDOWS_TESTING.md)。
