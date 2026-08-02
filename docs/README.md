# 文档索引

`codex-custom-subagents` 的 README 只保留项目简介和最短安装路径。配置、迁移、排错和测试细节按下面的入口查找。

## 安装与配置

- [安装、升级与卸载](INSTALLATION.md)：macOS / Windows 安装结果、凭证、doctor 和卸载。
- [模型与 fallback 配置](CONFIGURATION.md)：模型-协议选择、独立 fallback、manifest 和非交互命令。
- [模型、Provider 与协议适配](MODEL_ADAPTERS.md)：schema、Responses / Anthropic Messages 适配边界。
- [让 Codex 帮你安装](PROMPT_INSTALLATION.md)：可以直接发送给 Codex 的安全安装 Prompt。

## 架构与维护

- [系统架构](ARCHITECTURE.md)：模块职责、请求路径、单模型批次和任务信箱。
- [实现原理](IMPLEMENTATION.md)：安装事务、回执、fallback 状态和安全边界。
- [Skill 迁移](MIGRATION.md)：从旧 `$deepseek-delegation` 安装迁移到 `$codex-custom-agents`。
- [故障排查](TROUBLESHOOTING.md)：安装失败、agent 缓存、凭证和 adapter 日志。

## 测试

- [自动化与 Desktop 验收](TESTING.md)：单元测试、真实 worker 和 fallback 验收。
- [Windows 真机测试](WINDOWS_TESTING.md)：Task Scheduler、Credential Manager、重启和诊断包。
- [当前测试报告](../TEST_REPORT.md)：已经获得的证据和仍未验证的边界。

## 阅读顺序

普通安装：`INSTALLATION` → `CONFIGURATION` → `TESTING`。

旧版升级：`MIGRATION` → `INSTALLATION` → `TROUBLESHOOTING`。

新增协议：`ARCHITECTURE` → `MODEL_ADAPTERS` → `IMPLEMENTATION`。
