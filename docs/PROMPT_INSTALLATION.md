# 让 Codex 帮你安装

把下面的 Prompt 发送给 Codex。不要在 Prompt 中填写 API Key。

```text
请安装并配置 Codex Custom Subagents：
https://github.com/wongchisum/codex-custom-subagents

要求：
1. 检查操作系统、Python、Codex 和 Git。Windows 优先使用 py -3，并报告 sys.executable。
2. 阅读 README.md、docs/README.md、docs/INSTALLATION.md、docs/CONFIGURATION.md 和 docs/MIGRATION.md。
3. 先检查是否安装过旧的 deepseek-delegation。如果存在，使用项目迁移脚本预览；保留用户修改过的文件，不得直接递归删除旧 skill 目录。
4. 让我从以下 primary 模型-协议组合中选择一个：
   - deepseek (anthropic)
   - deepseek (openai)
   - gemini (anthropic)
   - claude (anthropic)
5. primary 确定后，单独询问是否配置 fallback；fallback 必须是有序列表，不能混进 primary profile 名称。
6. 只收集 base_url、remote_model、协议、上下文声明和凭证引用名称，不索取凭证值。
7. manifest 中不能包含 api_key、key、secret、token 或 value。
8. 如果 configure 返回 exit 3，原样展示交互式凭证命令并暂停，让我在本机终端输入。
9. 凭证保存后重复完全相同的 configure 命令，运行 doctor。
10. doctor 全部通过后，明确告诉我：当前 Codex 任务不会热加载新 agent，必须新建任务。
11. 新任务使用 $codex-custom-agents；不得继续使用旧的 $deepseek-delegation 名称。
12. 不提交、不推送、不修改 Git remote，不显示或转存任何凭证值。
```
