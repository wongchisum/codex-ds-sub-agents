"""Validate release assets so broken config cannot pass CI.

Runs standalone (python3 tests/test_release_assets.py) and as part of
unittest discovery. TOML parsing requires Python 3.11+ (tomllib), which is
what CI pins; on older interpreters the TOML checks are skipped.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MINIMAL_CLIENT_VERSION = "0.146.0"
TOML_REASON = "tomllib requires Python 3.11+; CI runs 3.11"


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
        skill = PROJECT_ROOT / "skills" / "deepseek-delegation" / "SKILL.md"
        self.assertTrue(skill.is_file())
        match = re.match(r"^---\n(.*?)\n---\n", skill.read_text(encoding="utf-8"), re.DOTALL)
        self.assertIsNotNone(match, "SKILL.md must start with YAML frontmatter")
        frontmatter = match.group(1)
        self.assertIn("name: deepseek-delegation", frontmatter)
        description = re.search(r"^description: (.+)$", frontmatter, re.MULTILINE)
        self.assertIsNotNone(description, "frontmatter must have a description")
        self.assertTrue(description.group(1).strip())
        self.assertTrue(
            (PROJECT_ROOT / "skills" / "deepseek-delegation" / "scripts" / "claim_task.py").is_file()
        )
        interface = (
            PROJECT_ROOT / "skills" / "deepseek-delegation" / "agents" / "openai.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn('display_name: "Custom Subagent Delegation"', interface)

    def test_minimum_version_is_consistent_across_repo(self) -> None:
        self.assertEqual(MINIMAL_CLIENT_VERSION, load_model_catalog()["models"][0]["minimal_client_version"])
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        readme_en = (PROJECT_ROOT / "README_EN.md").read_text(encoding="utf-8")
        self.assertIn(MINIMAL_CLIENT_VERSION, readme)
        self.assertIn(MINIMAL_CLIENT_VERSION, readme_en)
        self.assertNotIn("0.144.0", readme)
        self.assertNotIn("0.144.0", readme_en)

    def test_readmes_cross_link_and_reference_existing_screenshot(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        readme_en = (PROJECT_ROOT / "README_EN.md").read_text(encoding="utf-8")
        screenshot = PROJECT_ROOT / "assets" / "codex-custom-subagents.png"

        self.assertIn("[English](README_EN.md)", readme)
        self.assertIn("[中文](README.md)", readme_en)
        self.assertIn("assets/codex-custom-subagents.png", readme)
        self.assertIn("assets/codex-custom-subagents.png", readme_en)
        self.assertTrue(screenshot.is_file())
        self.assertEqual(b"\x89PNG\r\n\x1a\n", screenshot.read_bytes()[:8])

    def test_plugin_manifest_uses_custom_subagents_name(self) -> None:
        manifest_path = PROJECT_ROOT / ".codex-plugin" / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual("codex-custom-subagents", manifest["name"])
        self.assertEqual("Codex Custom Subagents", manifest["interface"]["displayName"])
        self.assertEqual("./skills/", manifest["skills"])

        for readme_name in ("README.md", "README_EN.md"):
            readme = (PROJECT_ROOT / readme_name).read_text(encoding="utf-8")
            self.assertIn("Codex Custom Subagents", readme)
            self.assertNotIn("github.com/wongchisum/codex-ds-sub-agents", readme)

    def test_worktree_and_delegations_are_ignored_forever(self) -> None:
        gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertRegex(gitignore, r"(?m)^/worktree/$")
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
        self.assertIn("python3 scripts/configure.py --profile", readme)
        self.assertIn("让 Codex 帮你安装和配置", readme)
        self.assertIn(
            "/config/*.local.json",
            (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
