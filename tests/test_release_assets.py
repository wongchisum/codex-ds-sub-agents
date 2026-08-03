"""Validate release assets so broken config cannot pass CI.

Runs standalone (python3 tests/test_release_assets.py) and as part of
unittest discovery. TOML parsing requires Python 3.11+ (tomllib), which is
what CI pins; on older interpreters the TOML checks are skipped.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from model_manifest import load_manifest  # noqa: E402


MINIMAL_CLIENT_VERSION = "0.146.0"
TOML_REASON = "tomllib requires Python 3.11+; CI runs 3.11"
BILINGUAL_DOCS = (
    "ARCHITECTURE.md",
    "CONFIGURATION.md",
    "IMPLEMENTATION.md",
    "INSTALLATION.md",
    "MIGRATION.md",
    "MODEL_ADAPTERS.md",
    "PROMPT_INSTALLATION.md",
    "README.md",
    "TESTING.md",
    "TROUBLESHOOTING.md",
    "WINDOWS_TESTING.md",
)


def load_model_catalog() -> dict:
    path = PROJECT_ROOT / "models" / "deepseek-v4-flash.json"
    return json.loads(path.read_text(encoding="utf-8"))


def rendered_agent_toml() -> str:
    template = (PROJECT_ROOT / "agents" / "deepseek-worker.toml.template").read_text(encoding="utf-8")
    rendered = template.replace("__CODEX_HOME__", "/tmp/codex-home")
    rendered = rendered.replace("__PYTHON_COMMAND__", '"/usr/bin/python3"')
    return rendered


class ReleaseAssetTests(unittest.TestCase):
    def test_model_catalog_is_valid_json_with_required_fields(self) -> None:
        model = load_model_catalog()["models"][0]
        self.assertEqual("deepseek-v4-flash", model["slug"])
        for key in (
            "minimal_client_version",
            "multi_agent_version",
            "context_window",
            "max_context_window",
            "display_name",
            "supported_reasoning_levels",
        ):
            self.assertIn(key, model)
        self.assertEqual(MINIMAL_CLIENT_VERSION, model["minimal_client_version"])

    @unittest.skipUnless(tomllib is not None, TOML_REASON)
    def test_provider_toml_is_valid(self) -> None:
        parsed = tomllib.loads((PROJECT_ROOT / "config" / "deepseek-provider.toml").read_text(encoding="utf-8"))
        provider = parsed["model_providers"]["deepseek"]
        self.assertEqual("https://api.deepseek.com", provider["base_url"])
        self.assertEqual("responses", provider["wire_api"])
        auth = provider["auth"]
        self.assertEqual("/usr/bin/security", auth["command"])
        self.assertIn("deepseek-api-key", auth["args"])

    @unittest.skipUnless(tomllib is not None, TOML_REASON)
    def test_rendered_agent_toml_is_valid(self) -> None:
        rendered = rendered_agent_toml()
        self.assertNotIn("__CODEX_HOME__", rendered)
        parsed = tomllib.loads(rendered)
        self.assertEqual("deepseek_worker", parsed["name"])
        self.assertEqual("deepseek-v4-flash", parsed["model"])
        self.assertEqual("deepseek", parsed["model_provider"])
        self.assertTrue(
            parsed["model_catalog_json"].endswith("/models/deepseek-v4-flash.json"),
            parsed["model_catalog_json"],
        )
        self.assertEqual("workspace-write", parsed["sandbox_mode"])
        self.assertIn("claim_task.py", parsed["developer_instructions"])

    def test_skill_frontmatter_is_valid(self) -> None:
        skill = PROJECT_ROOT / "skills" / "codex-custom-subagents" / "SKILL.md"
        self.assertTrue(skill.is_file())
        match = re.match(r"^---\n(.*?)\n---\n", skill.read_text(encoding="utf-8"), re.DOTALL)
        self.assertIsNotNone(match, "SKILL.md must start with YAML frontmatter")
        frontmatter = match.group(1)
        self.assertIn("name: codex-custom-subagents", frontmatter)
        description = re.search(r"^description: (.+)$", frontmatter, re.MULTILINE)
        self.assertIsNotNone(description, "frontmatter must have a description")
        self.assertTrue(description.group(1).strip())
        self.assertTrue(
            (PROJECT_ROOT / "skills" / "codex-custom-subagents" / "scripts" / "claim_task.py").is_file()
        )
        interface = (
            PROJECT_ROOT / "skills" / "codex-custom-subagents" / "agents" / "openai.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn('display_name: "Codex Custom Subagents"', interface)
        self.assertIn("$codex-custom-subagents", interface)
        self.assertFalse((PROJECT_ROOT / "skills" / "codex-custom-agents").exists())

    def test_minimum_version_is_consistent_across_repo(self) -> None:
        self.assertEqual(MINIMAL_CLIENT_VERSION, load_model_catalog()["models"][0]["minimal_client_version"])
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        readme_zh = (PROJECT_ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        self.assertIn(MINIMAL_CLIENT_VERSION, readme)
        self.assertIn(MINIMAL_CLIENT_VERSION, readme_zh)
        self.assertNotIn("0.144.0", readme)
        self.assertNotIn("0.144.0", readme_zh)

    def test_readmes_cross_link_and_reference_existing_screenshot(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        readme_zh = (PROJECT_ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        screenshot = PROJECT_ROOT / "assets" / "codex-custom-subagents.png"

        self.assertFalse((PROJECT_ROOT / "README_EN.md").exists())
        self.assertIn("English · [简体中文](README.zh-CN.md)", readme)
        self.assertIn("[English](README.md) · 简体中文", readme_zh)
        self.assertIn("assets/codex-custom-subagents.png", readme)
        self.assertIn("assets/codex-custom-subagents.png", readme_zh)
        self.assertTrue(screenshot.is_file())
        self.assertEqual(b"\x89PNG\r\n\x1a\n", screenshot.read_bytes()[:8])

    def test_readmes_lead_with_prompts_and_state_verified_capabilities(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        readme_zh = (PROJECT_ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

        self.assertIn("helps Codex Desktop use custom model providers as subagents", readme)
        capabilities = readme.index("## What it actually does")
        self.assertLess(readme.index("## Ask Codex to install it"), capabilities)
        self.assertLess(readme.index("## Use it in Codex"), capabilities)
        self.assertIn("macOS and Windows 10/11", readme)
        self.assertIn("It does not mean live task synchronization", readme)
        self.assertIn("`openai_responses`", readme)
        self.assertIn("`anthropic_messages`", readme)
        self.assertIn("JSON manifest schema v2", readme)
        self.assertIn("/.codex-custom-subagents/", readme)
        self.assertIn("帮助 Codex Desktop 使用自定义模型 Provider 作为 subagent", readme_zh)
        capabilities_zh = readme_zh.index("## 它实际做什么")
        self.assertLess(readme_zh.index("## 让 Codex 安装"), capabilities_zh)
        self.assertLess(readme_zh.index("## 在 Codex 中使用"), capabilities_zh)
        self.assertIn("macOS、Windows 10/11", readme_zh)
        self.assertIn("`openai_responses`", readme_zh)
        self.assertIn("`anthropic_messages`", readme_zh)
        self.assertIn("JSON manifest schema v2", readme_zh)
        self.assertIn("/.codex-custom-subagents/", readme_zh)

    def test_readme_custom_manifest_examples_match_and_validate(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        readme_zh = (PROJECT_ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        english = re.search(r"Minimal shape:\n\n```json\n(.*?)\n```", readme, re.DOTALL)
        chinese = re.search(r"最小结构：\n\n```json\n(.*?)\n```", readme_zh, re.DOTALL)
        self.assertIsNotNone(english)
        self.assertIsNotNone(chinese)
        english_manifest = json.loads(english.group(1))
        self.assertEqual(english_manifest, json.loads(chinese.group(1)))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readme-manifest.json"
            path.write_text(json.dumps(english_manifest), encoding="utf-8")
            manifest = load_manifest(path)

        self.assertEqual("my_model", manifest.selection.primary.id)
        self.assertEqual("openai_responses", manifest.providers["my_provider"].upstream_protocol)

    def test_documentation_defaults_to_english_with_chinese_mirrors(self) -> None:
        for name in BILINGUAL_DOCS:
            english_path = PROJECT_ROOT / "docs" / name
            chinese_path = PROJECT_ROOT / "docs" / "zh-CN" / name
            self.assertTrue(english_path.is_file(), english_path)
            self.assertTrue(chinese_path.is_file(), chinese_path)
            self.assertIn(
                f"[简体中文](zh-CN/{name})",
                english_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                f"[English](../{name})",
                chinese_path.read_text(encoding="utf-8"),
            )

        docs_index = (PROJECT_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
        self.assertIn("helps Codex Desktop use custom model providers as subagents", docs_index)
        self.assertLess(docs_index.index("## Start with Codex"), docs_index.index("## Install and configure"))

    def test_plugin_manifest_uses_custom_subagents_name(self) -> None:
        manifest_path = PROJECT_ROOT / ".codex-plugin" / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual("codex-custom-subagents", manifest["name"])
        self.assertEqual("Codex Custom Subagents", manifest["interface"]["displayName"])
        self.assertEqual("./skills/", manifest["skills"])
        self.assertIn(
            "$codex-custom-subagents",
            " ".join(manifest["interface"]["defaultPrompt"]),
        )
        self.assertIn("Codex Desktop subagents", manifest["description"])
        self.assertEqual(
            "Run Codex tasks with custom subagents.",
            manifest["interface"]["shortDescription"],
        )

        for readme_name in ("README.md", "README.zh-CN.md"):
            readme = (PROJECT_ROOT / readme_name).read_text(encoding="utf-8")
            self.assertIn("Codex Custom Subagents", readme)
            self.assertNotIn("github.com/wongchisum/codex-ds-sub-agents", readme)

    def test_worktree_and_mailboxes_are_ignored_forever(self) -> None:
        gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertRegex(gitignore, r"(?m)^/worktree/$")
        self.assertRegex(gitignore, r"(?m)^/\.codex-custom-subagents/$")
        self.assertRegex(gitignore, r"(?m)^/\.deepseek-delegations/$")

    def test_uninstall_script_exists_and_is_compilable(self) -> None:
        uninstall = PROJECT_ROOT / "scripts" / "uninstall.py"
        self.assertTrue(uninstall.is_file())
        source = uninstall.read_text(encoding="utf-8")
        self.assertIn("--dry-run", source)
        self.assertIn("preserved", source)
        compile(source, str(uninstall), "exec")

    def test_configure_entrypoint_is_documented_and_compilable(self) -> None:
        configure = PROJECT_ROOT / "scripts" / "configure.py"
        source = configure.read_text(encoding="utf-8")
        compile(source, str(configure), "exec")
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("python3 scripts/configure.py --primary", readme)
        self.assertIn("Ask Codex to install it", readme)
        self.assertIn(
            "/config/*.local.json",
            (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
