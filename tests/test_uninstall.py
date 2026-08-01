from __future__ import annotations

import subprocess
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import uninstall as uninstall_module  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "install.py"
UNINSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "uninstall.py"


def run(script: Path, codex_home: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), "--codex-home", str(codex_home), *extra],
        text=True,
        capture_output=True,
        check=False,
    )


def install(codex_home: Path) -> subprocess.CompletedProcess[str]:
    return run(INSTALL_SCRIPT, codex_home)


def uninstall(codex_home: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return run(UNINSTALL_SCRIPT, codex_home, *extra)


class UninstallTests(unittest.TestCase):
    def test_blank_codex_home_falls_back_to_default(self) -> None:
        with patch.dict(os.environ, {"CODEX_HOME": "  "}):
            with patch.object(sys, "argv", [str(UNINSTALL_SCRIPT)]):
                self.assertEqual(Path.home() / ".codex", uninstall_module.parse_args().codex_home)

    def test_uninstall_removes_installed_files_and_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            self.assertEqual(0, install(codex_home).returncode)

            result = uninstall(codex_home)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse((codex_home / "agents" / "deepseek-worker.toml").exists())
            self.assertFalse((codex_home / "models" / "deepseek-v4-flash.json").exists())
            self.assertFalse((codex_home / "skills" / "deepseek-delegation").exists())
            config = codex_home / "config.toml"
            self.assertTrue(config.exists())
            self.assertNotIn("[model_providers.deepseek]", config.read_text(encoding="utf-8"))

    def test_uninstall_preserves_modified_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            self.assertEqual(0, install(codex_home).returncode)
            agent = codex_home / "agents" / "deepseek-worker.toml"
            agent.write_text(agent.read_text(encoding="utf-8") + "\n# user tweak\n", encoding="utf-8")

            result = uninstall(codex_home)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(agent.is_file())
            self.assertIn("# user tweak", agent.read_text(encoding="utf-8"))
            self.assertIn("preserved", result.stdout)
            self.assertFalse((codex_home / "models" / "deepseek-v4-flash.json").exists())

    def test_uninstall_preserves_modified_provider_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            self.assertEqual(0, install(codex_home).returncode)
            config = codex_home / "config.toml"
            modified = config.read_text(encoding="utf-8").replace(
                'base_url = "https://api.deepseek.com"',
                'base_url = "https://api.deepseek.com/v1"',
            )
            config.write_text(modified, encoding="utf-8")

            result = uninstall(codex_home)

            self.assertEqual(0, result.returncode, result.stderr)
            content = config.read_text(encoding="utf-8")
            self.assertIn("[model_providers.deepseek]", content)
            self.assertIn('base_url = "https://api.deepseek.com/v1"', content)
            self.assertIn("preserved", result.stdout)

    def test_uninstall_preserves_user_skill_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            self.assertEqual(0, install(codex_home).returncode)
            extra = codex_home / "skills" / "deepseek-delegation" / "user-notes.md"
            extra.write_text("user content\n", encoding="utf-8")

            result = uninstall(codex_home)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(extra.is_file())
            self.assertFalse((codex_home / "skills" / "deepseek-delegation" / "SKILL.md").exists())
            self.assertIn("preserved", result.stdout)

    def test_uninstall_backs_up_config_before_removing_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            self.assertEqual(0, install(codex_home).returncode)

            result = uninstall(codex_home)

            self.assertEqual(0, result.returncode, result.stderr)
            backups = list(codex_home.glob("config.toml.bak.*"))
            self.assertEqual(1, len(backups))
            self.assertIn("[model_providers.deepseek]", backups[0].read_text(encoding="utf-8"))

    def test_uninstall_dry_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            self.assertEqual(0, install(codex_home).returncode)
            config_before = (codex_home / "config.toml").read_text(encoding="utf-8")

            result = uninstall(codex_home, "--dry-run")

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue((codex_home / "agents" / "deepseek-worker.toml").is_file())
            self.assertTrue((codex_home / "models" / "deepseek-v4-flash.json").is_file())
            self.assertTrue((codex_home / "skills" / "deepseek-delegation" / "SKILL.md").is_file())
            self.assertEqual(config_before, (codex_home / "config.toml").read_text(encoding="utf-8"))
            self.assertIn("would remove", result.stdout)

    def test_uninstall_without_install_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text("# user config only\n", encoding="utf-8")

            first = uninstall(codex_home)
            second = uninstall(codex_home)

            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertEqual("# user config only\n", (codex_home / "config.toml").read_text(encoding="utf-8"))

    def test_uninstall_preserves_symlink_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            outside = root / "outside-agent.toml"
            outside.write_text("keep", encoding="utf-8")
            (codex_home / "agents").mkdir()
            (codex_home / "agents" / "deepseek-worker.toml").symlink_to(outside)

            result = uninstall(codex_home)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue((codex_home / "agents" / "deepseek-worker.toml").is_symlink())
            self.assertEqual("keep", outside.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
