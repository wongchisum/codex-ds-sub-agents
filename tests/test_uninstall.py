from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import uninstall as uninstall_module  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "install.py"
UNINSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "uninstall.py"
DOCTOR_SCRIPT = PROJECT_ROOT / "scripts" / "doctor.py"


def run(script: Path, codex_home: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    arguments = [sys.executable, str(script), "--codex-home", str(codex_home), *extra]
    if "--manifest" in extra and script == INSTALL_SCRIPT:
        arguments.append("--no-start-adapters")
    if "--manifest" in extra and script == UNINSTALL_SCRIPT:
        arguments.append("--no-stop-adapters")
    return subprocess.run(
        arguments,
        text=True,
        capture_output=True,
        check=False,
    )


def install(codex_home: Path) -> subprocess.CompletedProcess[str]:
    return run(INSTALL_SCRIPT, codex_home)


def uninstall(codex_home: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return run(UNINSTALL_SCRIPT, codex_home, *extra)


class UninstallTests(unittest.TestCase):
    def test_reinstall_hint_uses_actual_interpreter_not_bare_python3(self) -> None:
        """Issue #2: runtime-generated commands must use sys.executable on Windows."""
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            codex_home.mkdir(parents=True)
            result = uninstall(codex_home)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn(f"reinstall with {sys.executable} scripts/install.py", result.stdout)
            self.assertNotIn("reinstall with python3 ", result.stdout)

    def test_stop_adapter_service_failure_aborts_custom_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            manifest = PROJECT_ROOT / "config" / "gemini-anthropic.example.json"
            failed = subprocess.CompletedProcess(
                args=["adapter_service.py"], returncode=2, stdout="", stderr="bootout failed"
            )
            with patch.object(uninstall_module.subprocess, "run", return_value=failed):
                with self.assertRaisesRegex(RuntimeError, "bootout failed"):
                    uninstall_module.stop_adapter_services(codex_home, manifest, False)

    def test_stop_adapter_services_passes_sorted_provider_filters(self) -> None:
        codex_home = Path("/tmp/codex-home")
        manifest = PROJECT_ROOT / "config" / "model-providers.example.json"
        passed = subprocess.CompletedProcess(
            args=["adapter_service.py"], returncode=0, stdout="", stderr=""
        )
        with patch.object(uninstall_module.subprocess, "run", return_value=passed) as run_service:
            uninstall_module.stop_adapter_services(
                codex_home,
                manifest,
                False,
                {"claudecode_gemini", "aicodemirror_claude"},
            )
        arguments = run_service.call_args.args[0]
        self.assertEqual(
            [
                "--provider",
                "aicodemirror_claude",
                "--provider",
                "claudecode_gemini",
            ],
            arguments[-4:],
        )

    def test_partial_overlap_stops_only_exclusive_adapter_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            multi_manifest = PROJECT_ROOT / "config" / "model-providers.example.json"
            gemini_manifest = PROJECT_ROOT / "config" / "gemini-anthropic.example.json"
            first = run(INSTALL_SCRIPT, codex_home, "--manifest", str(multi_manifest))
            second = run(INSTALL_SCRIPT, codex_home, "--manifest", str(gemini_manifest))
            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(0, second.returncode, second.stderr)

            output = StringIO()
            with patch.object(uninstall_module, "stop_adapter_services") as stop, redirect_stdout(output):
                result = uninstall_module.uninstall_custom(
                    codex_home,
                    multi_manifest,
                    False,
                )

            self.assertEqual(0, result)
            stop.assert_called_once_with(
                codex_home,
                multi_manifest.resolve(),
                False,
                {"aicodemirror_claude"},
            )
            self.assertIn(
                "preserved adapter services shared by another manifest: claudecode_gemini",
                output.getvalue(),
            )
            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertNotIn("[model_providers.aicodemirror_claude]", config)
            self.assertIn("[model_providers.claudecode_gemini]", config)
            self.assertFalse(
                (codex_home / "agents" / "aicodemirror_claude_worker.toml").exists()
            )
            self.assertTrue(
                (codex_home / "agents" / "claudecode_gemini_worker.toml").is_file()
            )

    def test_custom_manifest_uninstall_removes_only_managed_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            manifest = PROJECT_ROOT / "config" / "model-providers.example.json"
            installed = run(INSTALL_SCRIPT, codex_home, "--manifest", str(manifest))
            self.assertEqual(0, installed.returncode, installed.stderr)
            result = uninstall(codex_home, "--manifest", str(manifest))

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse((codex_home / "agents" / "aicodemirror_claude_worker.toml").exists())
            self.assertFalse((codex_home / "agents" / "claudecode_gemini_worker.toml").exists())
            self.assertFalse((codex_home / "models" / "subagent-selection.json").exists())
            self.assertFalse(
                (codex_home / "models" / "aicodemirror_claude--claude-opus-4-6.json").exists()
            )
            self.assertFalse((codex_home / "adapters" / "anthropic_adapter_protocol.py").exists())
            self.assertFalse((codex_home / "adapters" / "anthropic_responses_adapter.py").exists())
            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertNotIn("model_providers.aicodemirror_claude", config)
            self.assertNotIn("model_providers.claudecode_gemini", config)

    def test_overlapping_manifest_uninstall_restores_previous_installation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            multi_manifest = PROJECT_ROOT / "config" / "model-providers.example.json"
            gemini_manifest = PROJECT_ROOT / "config" / "gemini-anthropic.example.json"

            first = run(INSTALL_SCRIPT, codex_home, "--manifest", str(multi_manifest))
            second = run(INSTALL_SCRIPT, codex_home, "--manifest", str(gemini_manifest))
            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(0, second.returncode, second.stderr)

            result = uninstall(codex_home, "--manifest", str(gemini_manifest))

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(
                (codex_home / "agents" / "claudecode_gemini_worker.toml").is_file()
            )
            self.assertTrue(
                (
                    codex_home
                    / "models"
                    / "claudecode_gemini--gemini-3-5-flash.json"
                ).is_file()
            )
            restored = json.loads(
                (codex_home / "models" / "subagent-selection.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("claude-opus-4-6", restored["selection"]["primary"])
            self.assertEqual(
                ["gemini-3-5-flash"], restored["selection"]["fallbacks"]
            )
            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn("[model_providers.aicodemirror_claude]", config)
            self.assertIn("[model_providers.claudecode_gemini]", config)
            self.assertIn("shared by another manifest", result.stdout)

            doctor = subprocess.run(
                [
                    sys.executable,
                    str(DOCTOR_SCRIPT),
                    "--codex-home",
                    str(codex_home),
                    "--manifest",
                    str(multi_manifest),
                    "--skip-keychain",
                    "--skip-adapter-health",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, doctor.returncode, doctor.stdout + doctor.stderr)

    def test_missing_manifest_can_uninstall_from_record_without_stopping_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            manifest = root / "gemini.json"
            manifest.write_bytes(
                (PROJECT_ROOT / "config" / "gemini-anthropic.example.json").read_bytes()
            )
            installed = run(INSTALL_SCRIPT, codex_home, "--manifest", str(manifest))
            self.assertEqual(0, installed.returncode, installed.stderr)
            manifest.unlink()

            needs_manifest = subprocess.run(
                [
                    sys.executable,
                    str(UNINSTALL_SCRIPT),
                    "--codex-home",
                    str(codex_home),
                    "--manifest",
                    str(manifest),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(2, needs_manifest.returncode)
            self.assertIn("--no-stop-adapters", needs_manifest.stderr)
            self.assertTrue(
                (codex_home / "models" / "subagent-selection.json").is_file()
            )

            result = uninstall(codex_home, "--manifest", str(manifest))

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(
                (codex_home / "agents" / "claudecode_gemini_worker.toml").exists()
            )
            self.assertFalse(
                (codex_home / "models" / "subagent-selection.json").exists()
            )
            registry = uninstall_module.read_install_registry(codex_home)
            self.assertEqual({}, registry["installations"])
            self.assertEqual({}, registry["pending"])
            self.assertEqual([], registry["order"])

    def test_blank_codex_home_falls_back_to_default(self) -> None:
        with patch.dict(os.environ, {"CODEX_HOME": "  "}):
            with patch.object(sys, "argv", [str(UNINSTALL_SCRIPT)]):
                self.assertEqual(Path.home() / ".codex", uninstall_module.parse_args().codex_home)

    def test_custom_uninstall_preserves_legacy_installation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            manifest = PROJECT_ROOT / "config" / "deepseek-anthropic-1m.example.json"
            self.assertEqual(0, install(codex_home).returncode)
            self.assertEqual(
                0,
                run(INSTALL_SCRIPT, codex_home, "--manifest", str(manifest)).returncode,
            )

            result = uninstall(codex_home, "--manifest", str(manifest))

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue((codex_home / "agents" / "deepseek-worker.toml").is_file())
            self.assertTrue((codex_home / "models" / "deepseek-v4-flash.json").is_file())
            self.assertTrue((codex_home / "skills" / "codex-custom-subagents" / "SKILL.md").is_file())
            self.assertFalse((codex_home / "agents" / "deepseek_anthropic_worker.toml").exists())
            self.assertFalse(
                (codex_home / "models" / "deepseek_anthropic--deepseek-v4-flash.json").exists()
            )
            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn("[model_providers.deepseek]", config)
            self.assertNotIn("[model_providers.deepseek_anthropic]", config)
            self.assertIn("shared by another installation", result.stdout)

    def test_legacy_uninstall_preserves_custom_installation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            manifest = PROJECT_ROOT / "config" / "deepseek-anthropic-1m.example.json"
            self.assertEqual(0, install(codex_home).returncode)
            self.assertEqual(
                0,
                run(INSTALL_SCRIPT, codex_home, "--manifest", str(manifest)).returncode,
            )

            result = uninstall(codex_home)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse((codex_home / "agents" / "deepseek-worker.toml").exists())
            self.assertFalse((codex_home / "models" / "deepseek-v4-flash.json").exists())
            self.assertTrue((codex_home / "agents" / "deepseek_anthropic_worker.toml").is_file())
            self.assertTrue(
                (codex_home / "models" / "deepseek_anthropic--deepseek-v4-flash.json").is_file()
            )
            self.assertTrue((codex_home / "models" / "subagent-selection.json").is_file())
            self.assertTrue((codex_home / "skills" / "codex-custom-subagents" / "SKILL.md").is_file())
            self.assertTrue((codex_home / "adapters" / "anthropic_responses_adapter.py").is_file())
            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertNotIn("[model_providers.deepseek]", config)
            self.assertIn("[model_providers.deepseek_anthropic]", config)
            self.assertIn("shared by another installation", result.stdout)

    def test_uninstall_removes_installed_files_and_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            self.assertEqual(0, install(codex_home).returncode)

            result = uninstall(codex_home)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse((codex_home / "agents" / "deepseek-worker.toml").exists())
            self.assertFalse((codex_home / "models" / "deepseek-v4-flash.json").exists())
            self.assertFalse((codex_home / "skills" / "codex-custom-subagents").exists())
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
            extra = codex_home / "skills" / "codex-custom-subagents" / "user-notes.md"
            extra.write_text("user content\n", encoding="utf-8")

            result = uninstall(codex_home)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(extra.is_file())
            self.assertFalse((codex_home / "skills" / "codex-custom-subagents" / "SKILL.md").exists())
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
            self.assertTrue((codex_home / "skills" / "codex-custom-subagents" / "SKILL.md").is_file())
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
