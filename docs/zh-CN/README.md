[English](../README.md) · 简体中文

# 文档索引

`codex-custom-subagents` 帮助 Codex Desktop 在 macOS 和 Windows 上使用自定义模型 Provider 作为 subagent 完成任务。先看安装和使用 Prompt，再按需阅读各项功能文档。

## 从 Codex 开始

- [让 Codex 帮你安装](PROMPT_INSTALLATION.md)：可以直接发送给 Codex 的安全安装 Prompt。
- [README 使用 Prompt](../../README.zh-CN.md#在-codex-中使用)：安装后如何让 Codex 委派单个或并行任务。

## 插件实际提供什么

- 把仓库分析、边界明确的实现、测试和审查交给自定义 Provider 支持的 Codex
  subagent，父任务仍负责验收。
- 在 macOS 和 Windows 10/11 使用相同的安装与任务信箱流程。
- 直接连接兼容 OpenAI Responses 的上游，或通过本机 adapter 转换 Anthropic
  Messages。
- 用经过校验的 JSON manifest 自定义 Provider、模型、凭证引用、上下文声明、
  primary 和有序 fallback。

DeepSeek、Claude 和 Gemini preset 只是配置示例，不是唯一可用服务。实际要求是
上游实现当前两种协议之一。准确边界见[协议适配](MODEL_ADAPTERS.md)，完整字段见
[配置文档](CONFIGURATION.md)。

## 安装与配置

- [安装、升级与卸载](INSTALLATION.md)：macOS / Windows 安装结果、凭证、doctor 和卸载。
- [模型与 fallback 配置](CONFIGURATION.md)：模型-协议选择、独立 fallback、manifest 和非交互命令。
- [模型、Provider 与协议适配](MODEL_ADAPTERS.md)：schema、Responses / Anthropic Messages 适配边界。

## 架构与维护

- [系统架构](ARCHITECTURE.md)：模块职责、请求路径、单模型批次和任务信箱。
- [实现原理](IMPLEMENTATION.md)：安装事务、回执、fallback 状态和安全边界。
- [Skill 迁移](MIGRATION.md)：从旧 `$deepseek-delegation` 或 `$codex-custom-agents` 安装迁移到 `$codex-custom-subagents`。
- [故障排查](TROUBLESHOOTING.md)：安装失败、agent 缓存、凭证和 adapter 日志。

## 测试

- [自动化与 Desktop 验收](TESTING.md)：单元测试、真实 worker 和 fallback 验收。
- [Windows 真机测试](WINDOWS_TESTING.md)：Task Scheduler、Credential Manager、重启和诊断包。
- [当前测试报告](../../TEST_REPORT.md)：已经获得的证据和仍未验证的边界。

## 阅读顺序

普通安装：`INSTALLATION` → `CONFIGURATION` → `TESTING`。

旧版升级：`MIGRATION` → `INSTALLATION` → `TROUBLESHOOTING`。

新增协议：`ARCHITECTURE` → `MODEL_ADAPTERS` → `IMPLEMENTATION`。
