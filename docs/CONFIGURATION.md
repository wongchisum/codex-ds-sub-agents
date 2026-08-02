# 模型与 fallback 配置

## 内置模型-协议选择

```bash
python3 scripts/configure.py --list-model-protocols
```

| CLI 名称 | 显示名称 | 上游协议 |
| --- | --- | --- |
| `deepseek-anthropic` | deepseek (anthropic) | `anthropic_messages` |
| `deepseek-openai` | deepseek (openai) | `openai_responses` |
| `gemini-anthropic` | gemini (anthropic) | `anthropic_messages` |
| `claude-anthropic` | claude (anthropic) | `anthropic_messages` |

显示名称只是便捷预设。Provider 的真实抽象仍是 `upstream_protocol`，模型名称不会决定协议。

## primary 与 fallback

只配置 primary：

```bash
python3 scripts/configure.py --primary deepseek-anthropic
```

按顺序添加 fallback：

```bash
python3 scripts/configure.py \
  --primary claude-anthropic \
  --fallback gemini-anthropic \
  --fallback deepseek-openai
```

primary 和 fallback 不能重复。`max_switches` 由 fallback 数量生成。每批 sub-agent 始终使用同一个 active agent；fallback 是父任务在合格故障后启动新批次，不是在同一批次混用多个模型。

## 自定义 manifest

内置预设不满足需求时，复制 `config/*.example.json` 并修改以下字段：

- Provider：`base_url`、`upstream_protocol`、认证引用、重试和本机 adapter 端口。
- Model：`remote_model`、`agent`、上下文声明、输出上限和工具能力。
- Selection：一个 `primary`、有序 `fallbacks` 和允许切换次数。

```bash
python3 scripts/configure.py \
  --manifest config/my-team.local.json \
  --name my-team
```

manifest 只能保存凭证引用，不能保存 API Key。支持 `keychain` 和 `env` 引用。Anthropic Messages Provider 会经本机 adapter 转成 Codex 当前使用的 Responses 协议；多个 adapter 必须使用不同监听端口。

## 旧入口

`--profile` 暂时保留旧脚本兼容，不再作为新用户的主要配置入口。`claude-gemini` 这类把 fallback 写进 profile 名称的组合不会新增；新配置必须把 fallback 单独声明。
