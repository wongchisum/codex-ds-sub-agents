[English](../MODEL_ADAPTERS.md) · 简体中文

# 模型、Provider 与协议适配

## manifest 结构

manifest 由 `providers`、`models` 和 `selection` 三部分组成：

```json
{
  "schema_version": 2,
  "selection": {
    "primary": "model-a",
    "fallbacks": ["model-b"],
    "max_switches": 1
  },
  "providers": [],
  "models": []
}
```

Provider 声明连接与认证；Model 声明远端模型名、Codex agent 和上下文元数据；Selection 决定本次 sub-agent 批次的候选顺序。

`model.id` 是本地 selection key，例如 `gemini-3-5-flash`；`remote_model` 是实际发给上游的模型名，例如 `gemini-3.5-flash`。两者不要求相同。

## Provider 配置

```json
{
  "id": "example_provider",
  "name": "Example Provider",
  "base_url": "https://example.com/api",
  "protocol": "responses",
  "upstream_protocol": "openai_responses",
  "auth": {
    "type": "keychain",
    "service": "example-api-key",
    "account": "codex"
  },
  "request_max_retries": 2,
  "stream_max_retries": 2,
  "stream_idle_timeout_ms": 300000
}
```

约束：

- `id` 只允许小写字母、数字、下划线和连字符，最长 64。
- 远端 `base_url` 必须是绝对 HTTPS URL，不能携带用户信息、query 或 fragment。
- 当前支持的 Codex `protocol` 只有 `responses`。
- `upstream_protocol` 是供应商协议抽象，只允许 `openai_responses` 或 `anthropic_messages`；模型名不会决定协议。
- 重试次数最多 10；空闲超时范围是 1 秒到 1 小时。
- 认证可用 `keychain`、`env` 或 `env_header`，不能内联密钥值。

## Model 配置

```json
{
  "id": "model-a",
  "provider": "example_provider",
  "remote_model": "vendor-model-name",
  "agent": "example_worker",
  "reasoning_effort": "high",
  "display_name": "Example Model",
  "context_window": 200000,
  "max_context_window": 200000,
  "effective_context_window_percent": 95,
  "supports_parallel_tool_calls": true,
  "supports_search_tool": false
}
```

上下文值来自用户对供应商模型的声明，安装器不会写死供应商上限。运行时应再用 `codex debug models` 和 `session_audit.py` 核验。worker 无法自省时必须报告“不可用”，不能把 catalog 值冒充实际值。

## Anthropic Messages adapter

需要转换的 Provider 只需声明：

```json
{
  "upstream_protocol": "anthropic_messages",
  "local_adapter": {
    "listen_host": "127.0.0.1",
    "listen_port": 18766,
    "max_output_tokens": 4096
  }
}
```

`local_adapter` 是可选运行参数，不是协议类型。省略时安装器使用 loopback、自动分配端口和 16384 输出上限。安装器生成的 Codex provider 指向本机 Responses 服务；adapter 把请求转成 Anthropic Messages。真实上游仍使用 Provider 的 `base_url`。

`schema_version: 1` 的 `adapter.type: anthropic_messages` 继续兼容。新配置不得继续复制该结构；`schema_version: 2` 会拒绝旧字段，避免协议声明存在两套来源。

### 输入转换

- Responses `instructions` 与 system 内容合并为 Anthropic `system`。
- 普通 user/assistant 文本映射为 Anthropic message blocks。
- Codex 原生 `agent_message` 中的明文 `input_text` 或 `text` 会转发。
- `encrypted_content` 不解密、不转发；只有密文而没有明文时返回协议错误。
- function tool 映射为 Anthropic tool；freeform `apply_patch` 映射为单字符串 `input`。
- `multi_agent_v1`、`web_search`、`view_image` 和 `request_user_input` 被过滤。
- `reasoning.effort` 映射到 `output_config.effort`；`xhigh` 与 `ultra` 降为上游 `max`。

### 输出转换

- Anthropic 文本映射为 Responses `message`。
- `tool_use` 根据原始工具类型映射为 `function_call` 或 `custom_tool_call`，保留 `call_id`。
- 非流式和 SSE 流式响应都受支持；客户端断开时关闭上游响应。
- 上游只有空白文本且没有工具调用时返回 HTTP 502 `upstream_invalid_response`。
- 请求 token 上限取 Codex 请求值、adapter `max_output_tokens` 和硬上限 64000 的最小值。

Gemini 示例把单次输出限制为 4096。按默认两次 transport retry 计算，同一失败请求最多可能向上游申请三次，即累计 12288 个输出 token；这不是单次响应上限。

## fallback 规则

允许切换的耗尽错误：

- `network`
- `timeout`
- `rate_limit`
- `billing`
- `service_unavailable`

禁止切换：

- `auth`
- `invalid_request`
- `model_not_found`
- `task_failure`
- 未知错误

认证或模型名错误切换到另一个模型只会掩盖错误配置，因此必须直接阻塞并报告。fallback 不是每个 HTTP 错误都自动重试的兜底。

## 新增适配器

新增协议时保持两层边界：

1. `model_manifest.py` 负责声明和严格校验 `upstream_protocol`，不保存协议实现。
2. 独立协议模块把 Responses 请求与响应转换为目标协议；HTTP 服务只负责认证转发、SSE、超时、审计和错误状态。

必须补充：输入/输出转换单元测试、工具循环测试、流中断测试、空响应测试、重定向凭证测试、`/health` 身份测试、安装/卸载服务测试，以及真实 Desktop worker 的端到端报告。
