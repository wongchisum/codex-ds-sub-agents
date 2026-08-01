from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "install.py"
DOCTOR_SCRIPT = PROJECT_ROOT / "scripts" / "doctor.py"
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import install  # noqa: E402


def run_install(codex_home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(INSTALL_SCRIPT), "--codex-home", str(codex_home)],
        text=True,
        capture_output=True,
        check=False,
    )


class InstallTests(unittest.TestCase):
    def test_install_is_repeatable_and_renders_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            first = run_install(codex_home)
            second = run_install(codex_home)

            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(0, second.returncode, second.stderr)
            agent = (codex_home / "agents" / "deepseek-worker.toml").read_text(encoding="utf-8")
            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertNotIn("__CODEX_HOME__", agent)
            self.assertIn(str(codex_home), agent)
            real_headers = [
                line for line in config.splitlines() if line.strip() == "[model_providers.deepseek]"
            ]
            self.assertEqual(1, len(real_headers))
            self.assertTrue((codex_home / "models" / "deepseek-v4-flash.json").is_file())
            self.assertTrue((codex_home / "skills" / "deepseek-delegation" / "SKILL.md").is_file())

    def test_codex_home_falls_back_when_env_is_blank(self) -> None:
        with patch.dict(os.environ, {"CODEX_HOME": "   "}):
            self.assertEqual(Path.home() / ".codex", install.resolve_codex_home())
        with patch.dict(os.environ, {"CODEX_HOME": ""}):
            self.assertEqual(Path.home() / ".codex", install.resolve_codex_home())
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Path.home() / ".codex", install.resolve_codex_home())
        with patch.dict(os.environ, {"CODEX_HOME": "/tmp/custom-codex"}):
            self.assertEqual(Path("/tmp/custom-codex"), install.resolve_codex_home())

    def test_toml_header_present_ignores_comments_and_strings(self) -> None:
        self.assertFalse(install.toml_header_present("# [model_providers.deepseek]", "[model_providers.deepseek]"))
        self.assertFalse(install.toml_header_present("[model_providers.deepseek.auth]", "[model_providers.deepseek]"))
        self.assertFalse(
            install.toml_header_present('description = "[model_providers.deepseek]"', "[model_providers.deepseek]")
        )
        self.assertTrue(install.toml_header_present("[model_providers.deepseek]", "[model_providers.deepseek]"))
        self.assertTrue(install.toml_header_present("[model_providers.deepseek] # trailing", "[model_providers.deepseek]"))
        self.assertTrue(
            install.toml_header_present(
                "# commented\n\n  [model_providers.deepseek]  ",
                "[model_providers.deepseek]",
            )
        )

    def test_install_does_not_create_worktree(self) -> None:
        worktree = PROJECT_ROOT / "worktree"
        before = sorted(path.relative_to(worktree).as_posix() for path in worktree.rglob("*")) if worktree.exists() else None
        with tempfile.TemporaryDirectory() as directory:
            result = run_install(Path(directory) / "codex-home")
            self.assertEqual(0, result.returncode, result.stderr)
        after = sorted(path.relative_to(worktree).as_posix() for path in worktree.rglob("*")) if worktree.exists() else None
        self.assertEqual(before, after)

    def test_provider_detection_ignores_commented_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(
                "# [model_providers.deepseek]\n# [model_providers.deepseek.auth]\n", encoding="utf-8"
            )
            result = run_install(codex_home)
            self.assertEqual(0, result.returncode, result.stderr)
            content = (codex_home / "config.toml").read_text(encoding="utf-8")
            real_headers = [
                line for line in content.splitlines() if line.strip() == "[model_providers.deepseek]"
            ]
            self.assertEqual(1, len(real_headers))
            self.assertTrue(install.toml_header_present(content, "[model_providers.deepseek]"))
            self.assertTrue(install.toml_header_present(content, "[model_providers.deepseek.auth]"))

    def test_refuses_to_write_through_symlinked_skill_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            marker = outside / "marker.txt"
            marker.write_text("keep", encoding="utf-8")
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "skills").mkdir()
            (codex_home / "skills" / "deepseek-delegation").symlink_to(outside, target_is_directory=True)

            result = run_install(codex_home)

            self.assertEqual(2, result.returncode)
            self.assertIn("symlink", result.stderr)
            self.assertEqual("keep", marker.read_text(encoding="utf-8"))
            self.assertFalse((outside / "SKILL.md").exists())

    def test_refuses_to_write_through_symlinked_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside_config = root / "outside-config.toml"
            outside_config.write_text("original", encoding="utf-8")
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").symlink_to(outside_config)

            result = run_install(codex_home)

            self.assertEqual(2, result.returncode)
            self.assertIn("symlink", result.stderr)
            self.assertEqual("original", outside_config.read_text(encoding="utf-8"))

    def test_skill_upgrade_removes_only_manifested_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            first = run_install(codex_home)
            self.assertEqual(0, first.returncode, first.stderr)
            skill_dest = codex_home / "skills" / "deepseek-delegation"
            manifest_path = skill_dest / install.SKILL_MANIFEST
            manifest = install.json.loads(manifest_path.read_text(encoding="utf-8"))
            stale = skill_dest / "obsolete.txt"
            stale.write_text("old installed content", encoding="utf-8")
            manifest["files"]["obsolete.txt"] = install.file_digest(stale)
            manifest_path.write_text(install.json.dumps(manifest), encoding="utf-8")
            user_file = skill_dest / "user-notes.md"
            user_file.write_text("keep", encoding="utf-8")
            sibling = codex_home / "skills" / "unrelated.md"
            sibling.write_text("keep", encoding="utf-8")
            outside = codex_home / "keep.txt"
            outside.write_text("keep", encoding="utf-8")

            result = run_install(codex_home)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(stale.exists())
            self.assertEqual("keep", user_file.read_text(encoding="utf-8"))
            self.assertTrue((skill_dest / "SKILL.md").is_file())
            self.assertTrue((skill_dest / "scripts" / "claim_task.py").is_file())
            self.assertEqual("keep", sibling.read_text(encoding="utf-8"))
            self.assertEqual("keep", outside.read_text(encoding="utf-8"))

    def test_doctor_passes_fresh_install_file_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            install_result = run_install(codex_home)
            self.assertEqual(0, install_result.returncode, install_result.stderr)
            result = subprocess.run(
                [sys.executable, str(DOCTOR_SCRIPT), "--codex-home", str(codex_home), "--skip-keychain"],
                text=True,
                capture_output=True,
                check=False,
            )
            for line in (
                "PASS  agent",
                "PASS  model",
                "PASS  skill",
                "PASS  claim script",
                "PASS  config",
                "PASS  provider",
                "PASS  provider auth",
            ):
                self.assertIn(line, result.stdout)

    def test_doctor_rejects_commented_provider_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            install_result = run_install(codex_home)
            self.assertEqual(0, install_result.returncode, install_result.stderr)
            (codex_home / "config.toml").write_text(
                "# [model_providers.deepseek]\n# [model_providers.deepseek.auth]\n", encoding="utf-8"
            )
            result = subprocess.run(
                [sys.executable, str(DOCTOR_SCRIPT), "--codex-home", str(codex_home), "--skip-keychain"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertIn("FAIL  provider", result.stdout)
            self.assertIn("FAIL  provider auth", result.stdout)


if __name__ == "__main__":
    unittest.main()
