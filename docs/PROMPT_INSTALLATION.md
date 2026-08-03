# Ask Codex to install the plugin

English · [简体中文](zh-CN/PROMPT_INSTALLATION.md)

Open this repository in Codex Desktop and send the following prompt. Do not put an API key in the prompt.

```text
Install and configure Codex Custom Subagents from this repository:
https://github.com/wongchisum/codex-custom-subagents

1. Detect the operating system, Python interpreter, Codex version, and Git.
   Use py -3 on Windows and python3 on macOS, and report sys.executable.
2. Read README.md, docs/README.md, docs/INSTALLATION.md,
   docs/CONFIGURATION.md, and docs/MIGRATION.md before changing files.
3. Check for managed legacy skills named deepseek-delegation or
   codex-custom-agents. Run the migration preview first, preserve modified or
   unknown files, and never recursively delete an old skill directory.
4. Ask me to choose one primary model/protocol preset:
   - deepseek (anthropic)
   - deepseek (openai)
   - gemini (anthropic)
   - claude (anthropic)
5. After I choose the primary, ask separately whether I want ordered fallbacks.
6. Collect only provider URLs, remote model names, protocol choices, context
   declarations, output limits, and credential reference names. Never request
   a credential value.
7. A manifest must not contain api_key, key, secret, token, or value fields.
8. Run the explicit configure.py command for my choices. If it exits with code
   3, show the printed interactive credential command and wait while I execute
   it locally.
9. Re-run the same configure command after credentials are stored and run
   doctor. Report success only when doctor passes.
10. Tell me to close this Codex task and start a new task because open tasks do
    not hot-load newly installed agent types.
11. In the new task, use $codex-custom-subagents. Do not use any legacy
    skill name.
12. Do not print, copy, commit, upload, or otherwise expose credential values.
```

After installation, use the task prompt in the root [README](../README.md#use-it-in-codex).
