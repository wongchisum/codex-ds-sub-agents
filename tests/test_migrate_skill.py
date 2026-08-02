"""Migration tests for the `deepseek-delegation` → `codex-custom-agents` skill rename."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATE_SCRIPT = PROJECT_ROOT / "scripts" / "migrate_skill.py"
UNINSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "uninstall.py"
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import install  # noqa: E402

LEGACY_SKILL = install.LEGACY_SKILL_NAME
NEW_SKILL = install.SKILL_NAME


def run_migrate(codex_home: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MIGRATE_SCRIPT), "--codex-home", str(codex_home), *extra],
        text=True,
        capture_output=True,
        check=False,
    )


def run_uninstall(codex_home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(UNINSTALL_SCRIPT), "--codex-home", str(codex_home)],
        text=True,
        capture_output=True,
        check=False,
    )


def make_legacy_install(codex_home: Path) -> Path:
    """Create a legacy managed skill dir whose manifest records exact digests."""
    legacy = codex_home / "skills" / LEGACY_SKILL
    files = {
        "SKILL.md": "---\nname: deepseek-delegation\n---\nlegacy body\n",
        "scripts/claim_task.py": "# legacy claim script\n",
        "agents/openai.yaml": "interface:\n  display_name: Legacy\n",
    }
    for relative, content in files.items():
        path = legacy / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    manifest = {
        "version": 1,
        "files": {relative: install.file_digest(legacy / relative) for relative in files},
    }
    (legacy / install.SKILL_MANIFEST).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return legacy


class MigrateSkillTests(unittest.TestCase):
    def test_fresh_install_uses_new_skill_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            result = run_migrate(codex_home)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("no managed legacy", result.stdout)
            self.assertTrue((codex_home / "skills" / NEW_SKILL / "SKILL.md").is_file())
            self.assertFalse((codex_home / "skills" / LEGACY_SKILL).exists())
            agent = (codex_home / "agents" / "deepseek-worker.toml").read_text(encoding="utf-8")
            self.assertIn(f"{codex_home}/skills/{NEW_SKILL}/scripts/claim_task.py", agent)
            self.assertNotIn(f"skills/{LEGACY_SKILL}/scripts/claim_task.py", agent)

    def test_migrates_managed_legacy_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            legacy = make_legacy_install(codex_home)
            self.assertTrue((legacy / "SKILL.md").is_file())

            result = run_migrate(codex_home)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("removed", result.stdout)
            self.assertFalse(legacy.exists())
            self.assertTrue((codex_home / "skills" / NEW_SKILL / "SKILL.md").is_file())
            skill_text = (codex_home / "skills" / NEW_SKILL / "SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("name: codex-custom-agents", skill_text)

    def test_preserves_modified_legacy_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            legacy = make_legacy_install(codex_home)
            modified = legacy / "SKILL.md"
            modified.write_text("user modified content\n", encoding="utf-8")

            result = run_migrate(codex_home)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("preserved", result.stdout)
            self.assertEqual("user modified content\n", modified.read_text(encoding="utf-8"))
            self.assertFalse((legacy / "scripts" / "claim_task.py").exists())
            self.assertFalse((legacy / install.SKILL_MANIFEST).exists())
            self.assertTrue(legacy.is_dir())
            self.assertTrue((codex_home / "skills" / NEW_SKILL / "SKILL.md").is_file())

    def test_dry_run_previews_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            legacy = make_legacy_install(codex_home)
            before = {p: p.read_bytes() for p in legacy.rglob("*") if p.is_file()}

            result = run_migrate(codex_home, "--dry-run")

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("would remove", result.stdout)
            after = {p: p.read_bytes() for p in legacy.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertFalse((codex_home / "skills" / NEW_SKILL).exists())

    def test_second_run_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            first = run_migrate(codex_home)
            second = run_migrate(codex_home)

            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertIn("no managed legacy", second.stdout)
            self.assertTrue((codex_home / "skills" / NEW_SKILL / "SKILL.md").is_file())
            self.assertFalse((codex_home / "skills" / LEGACY_SKILL).exists())

    def test_registered_custom_agents_are_rerendered_to_new_skill_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            manifest = PROJECT_ROOT / "config" / "deepseek-openai.example.json"
            install.install_custom_manifest(codex_home, manifest, start_adapters=False)
            agent = codex_home / "agents" / "deepseek_openai_worker.toml"
            current = agent.read_text(encoding="utf-8")
            agent.write_text(
                current.replace(
                    f"skills/{NEW_SKILL}/scripts/claim_task.py",
                    f"skills/{LEGACY_SKILL}/scripts/claim_task.py",
                ),
                encoding="utf-8",
            )
            make_legacy_install(codex_home)

            result = run_migrate(codex_home)

            self.assertEqual(0, result.returncode, result.stderr)
            rendered = agent.read_text(encoding="utf-8")
            self.assertIn(f"skills/{NEW_SKILL}/scripts/claim_task.py", rendered)
            self.assertNotIn(f"skills/{LEGACY_SKILL}/scripts/claim_task.py", rendered)
            self.assertIn("1 registered manifest installation(s) re-rendered", result.stdout)

    def test_preserves_unmanaged_legacy_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            legacy = codex_home / "skills" / LEGACY_SKILL
            (legacy / "scripts").mkdir(parents=True)
            (legacy / "SKILL.md").write_text("manual copy\n", encoding="utf-8")

            result = run_migrate(codex_home)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("no managed manifest", result.stdout)
            self.assertEqual("manual copy\n", (legacy / "SKILL.md").read_text(encoding="utf-8"))
            self.assertTrue((codex_home / "skills" / NEW_SKILL / "SKILL.md").is_file())

    def test_uninstall_removes_owned_files_and_preserves_user_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            migrated = run_migrate(codex_home)
            self.assertEqual(0, migrated.returncode, migrated.stderr)
            legacy = make_legacy_install(codex_home)
            user_file = legacy / "user-notes.md"
            user_file.write_text("keep\n", encoding="utf-8")

            result = run_uninstall(codex_home)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse((codex_home / "skills" / NEW_SKILL).exists())
            self.assertFalse((legacy / "SKILL.md").exists())
            self.assertFalse((legacy / "scripts" / "claim_task.py").exists())
            self.assertFalse((legacy / install.SKILL_MANIFEST).exists())
            self.assertEqual("keep\n", user_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
