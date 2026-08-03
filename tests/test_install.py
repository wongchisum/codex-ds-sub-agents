from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_support import create_symlink_or_skip


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "install.py"
UNINSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "uninstall.py"
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


def run_manifest_install(codex_home: Path, manifest: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(INSTALL_SCRIPT),
            "--codex-home",
            str(codex_home),
            "--manifest",
            str(manifest),
            "--no-start-adapters",
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def run_manifest_uninstall(codex_home: Path, manifest: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(UNINSTALL_SCRIPT),
            "--codex-home",
            str(codex_home),
            "--manifest",
            str(manifest),
            "--no-stop-adapters",
        ],
        text=True,
        capture_output=True,
        check=False,
    )


class InstallTests(unittest.TestCase):
    def test_skill_install_ignores_python_cache_and_preserves_binary_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_root = root / "project"
            source = project_root / "skills" / install.SKILL_NAME
            (source / "scripts" / "__pycache__").mkdir(parents=True)
            (source / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
            (source / "scripts" / "worker.py").write_text("VALUE = 1\n", encoding="utf-8")
            (source / "assets").mkdir()
            (source / "assets" / "sample.bin").write_bytes(b"\xff\x00\x80")
            (source / "scripts" / "__pycache__" / "worker.cpython-313.pyc").write_bytes(
                b"\xf3\r\r\ncache"
            )
            (source / ".DS_Store").write_bytes(b"finder")
            codex_home = root / "codex-home"

            with patch.object(install, "PROJECT_ROOT", project_root):
                install.install_skill(codex_home)
                install.install_skill(codex_home)

            destination = codex_home / "skills" / install.SKILL_NAME
            self.assertEqual(b"\xff\x00\x80", (destination / "assets" / "sample.bin").read_bytes())
            self.assertFalse((destination / "scripts" / "__pycache__").exists())
            self.assertFalse((destination / ".DS_Store").exists())
            manifest = install.read_skill_manifest(destination / install.SKILL_MANIFEST)
            self.assertIn("assets/sample.bin", manifest)
            self.assertNotIn("scripts/__pycache__/worker.cpython-313.pyc", manifest)

    def test_adapter_service_start_failure_aborts_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            manifest = PROJECT_ROOT / "config" / "gemini-anthropic.example.json"
            failed = subprocess.CompletedProcess(
                args=["adapter_service.py"], returncode=2, stdout="", stderr="launchctl failed"
            )
            with patch.object(install.subprocess, "run", return_value=failed):
                with self.assertRaisesRegex(install.InstallError, "launchctl failed"):
                    install.start_adapter_services(codex_home, manifest)

    def test_adapter_start_failure_does_not_create_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            manifest = PROJECT_ROOT / "config" / "gemini-anthropic.example.json"

            with patch.object(
                install,
                "start_adapter_services",
                side_effect=install.InstallError("adapter start failed"),
            ):
                with self.assertRaisesRegex(install.InstallError, "adapter start failed"):
                    install.install_custom_manifest(codex_home, manifest)

            selection = codex_home / "models" / "subagent-selection.json"
            self.assertFalse(selection.exists())

    def test_adapter_start_failure_preserves_existing_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            selection = codex_home / "models" / "subagent-selection.json"
            selection.parent.mkdir(parents=True)
            previous = b'{"selection": {"primary": "existing-model"}}\n'
            selection.write_bytes(previous)
            manifest = PROJECT_ROOT / "config" / "gemini-anthropic.example.json"

            with patch.object(
                install,
                "start_adapter_services",
                side_effect=install.InstallError("adapter start failed"),
            ):
                with self.assertRaisesRegex(install.InstallError, "adapter start failed"):
                    install.install_custom_manifest(codex_home, manifest)

            self.assertEqual(previous, selection.read_bytes())
            self.assertEqual([], list(selection.parent.glob("subagent-selection.json.bak.*")))

    def test_selection_symlink_is_rejected_before_registry_or_install_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            selection = codex_home / "models" / "subagent-selection.json"
            selection.parent.mkdir(parents=True)
            outside = root / "existing-selection.json"
            previous = b'{"selection": {"primary": "existing-model"}}\n'
            outside.write_bytes(previous)
            create_symlink_or_skip(self, selection, outside)
            manifest = PROJECT_ROOT / "config" / "gemini-anthropic.example.json"

            with self.assertRaisesRegex(install.InstallError, "symlink"):
                install.install_custom_manifest(
                    codex_home,
                    manifest,
                    start_adapters=False,
                )

            registry = install.read_install_registry(codex_home)
            self.assertEqual({}, registry["installations"])
            self.assertEqual({}, registry["pending"])
            self.assertEqual([], registry["order"])
            self.assertEqual(previous, outside.read_bytes())
            self.assertFalse((codex_home / "skills").exists())

    def test_registry_commit_failure_restores_existing_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            existing_manifest = PROJECT_ROOT / "config" / "model-providers.example.json"
            install.install_custom_manifest(
                codex_home,
                existing_manifest,
                start_adapters=False,
            )
            selection = codex_home / "models" / "subagent-selection.json"
            previous = selection.read_bytes()
            previous_registry = install.read_install_registry(codex_home)
            manifest = PROJECT_ROOT / "config" / "gemini-anthropic.example.json"

            with patch.object(
                install,
                "activate_manifest_installation",
                side_effect=OSError("registry write failed"),
            ):
                with self.assertRaisesRegex(install.InstallError, "registry activation failed"):
                    install.install_custom_manifest(
                        codex_home,
                        manifest,
                        start_adapters=False,
                    )

            registry = install.read_install_registry(codex_home)
            self.assertEqual(
                previous_registry["installations"], registry["installations"]
            )
            self.assertEqual(previous_registry["order"], registry["order"])
            self.assertEqual(1, len(registry["pending"]))
            self.assertEqual(previous, selection.read_bytes())
            pending_record = next(iter(registry["pending"].values()))
            for relative in pending_record["files"]:
                self.assertTrue((codex_home / relative).is_file())

            cleanup = run_manifest_uninstall(codex_home, manifest)
            self.assertEqual(0, cleanup.returncode, cleanup.stderr)
            cleaned_registry = install.read_install_registry(codex_home)
            self.assertEqual(
                previous_registry["installations"],
                cleaned_registry["installations"],
            )
            self.assertEqual({}, cleaned_registry["pending"])
            self.assertEqual(previous, selection.read_bytes())

    def test_registry_commit_failure_removes_first_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            manifest = PROJECT_ROOT / "config" / "gemini-anthropic.example.json"

            with patch.object(
                install,
                "activate_manifest_installation",
                side_effect=OSError("registry write failed"),
            ):
                with self.assertRaisesRegex(install.InstallError, "registry activation failed"):
                    install.install_custom_manifest(
                        codex_home,
                        manifest,
                        start_adapters=False,
                    )

            selection = codex_home / "models" / "subagent-selection.json"
            registry = install.read_install_registry(codex_home)
            self.assertFalse(selection.exists())
            self.assertEqual({}, registry["installations"])
            self.assertEqual(1, len(registry["pending"]))
            self.assertEqual([], registry["order"])
            pending_record = next(iter(registry["pending"].values()))
            for relative in pending_record["files"]:
                self.assertTrue((codex_home / relative).is_file())

            cleanup = run_manifest_uninstall(codex_home, manifest)
            self.assertEqual(0, cleanup.returncode, cleanup.stderr)
            cleaned_registry = install.read_install_registry(codex_home)
            self.assertEqual({}, cleaned_registry["installations"])
            self.assertEqual({}, cleaned_registry["pending"])

    def test_custom_manifest_installs_two_candidates_and_one_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            manifest = PROJECT_ROOT / "config" / "model-providers.example.json"
            first = run_manifest_install(codex_home, manifest)
            second = run_manifest_install(codex_home, manifest)

            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(0, second.returncode, second.stderr)
            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertEqual(1, config.count("[model_providers.aicodemirror_claude]"))
            self.assertEqual(1, config.count("[model_providers.claudecode_gemini]"))
            self.assertTrue((codex_home / "agents" / "aicodemirror_claude_worker.toml").is_file())
            self.assertTrue((codex_home / "agents" / "claudecode_gemini_worker.toml").is_file())
            self.assertTrue(
                (codex_home / "models" / "aicodemirror_claude--claude-opus-4-6.json").is_file()
            )
            self.assertTrue((codex_home / "adapters" / "anthropic_adapter_protocol.py").is_file())
            self.assertTrue((codex_home / "adapters" / "anthropic_responses_adapter.py").is_file())
            self.assertIn("start adapter:", first.stdout)
            selection = install.json.loads(
                (codex_home / "models" / "subagent-selection.json").read_text(encoding="utf-8")
            )
            self.assertEqual("claude-opus-4-6", selection["selection"]["primary"])
            self.assertEqual(["gemini-3-5-flash"], selection["selection"]["fallbacks"])
            self.assertNotIn("sk-", config)

    def test_gemini_manifest_installs_one_selected_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            manifest = PROJECT_ROOT / "config" / "gemini-anthropic.example.json"
            result = run_manifest_install(codex_home, manifest)

            self.assertEqual(0, result.returncode, result.stderr)
            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn("[model_providers.claudecode_gemini]", config)
            self.assertIn('base_url = "http://127.0.0.1:18768"', config)
            self.assertTrue((codex_home / "agents" / "claudecode_gemini_worker.toml").is_file())
            selection = install.json.loads(
                (codex_home / "models" / "subagent-selection.json").read_text(encoding="utf-8")
            )
            self.assertEqual("gemini-3-5-flash", selection["selection"]["primary"])
            self.assertEqual([], selection["selection"]["fallbacks"])
            self.assertIn("--port 18768", result.stdout)
            self.assertIn("--service-id claudecode_gemini", result.stdout)
            self.assertIn("--max-output-tokens 4096", result.stdout)

    def test_manifest_install_lists_agents_and_requires_new_task(self) -> None:
        """Issue #2: successful output must name installed agents and require a NEW task."""
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            manifest = PROJECT_ROOT / "config" / "model-providers.example.json"
            result = run_manifest_install(codex_home, manifest)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("installed agents: aicodemirror_claude_worker, claudecode_gemini_worker", result.stdout)
            self.assertIn("NEW Codex task", result.stdout)
            self.assertIn("unknown agent_type", result.stdout)

    def test_legacy_install_lists_agent_and_requires_new_task(self) -> None:
        """Issue #2: legacy output must name the installed agent and require a NEW task."""
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            result = run_install(codex_home)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("installed agents: deepseek_worker", result.stdout)
            self.assertIn("NEW Codex task", result.stdout)
            self.assertIn("unknown agent_type", result.stdout)

    def test_custom_manifest_rejects_unsupported_runtime_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = install.json.loads(
                (PROJECT_ROOT / "config" / "model-providers.example.json").read_text(encoding="utf-8")
            )
            data["providers"][0]["protocol"] = "messages"
            manifest = root / "manifest.json"
            manifest.write_text(install.json.dumps(data), encoding="utf-8")
            result = run_manifest_install(root / "codex-home", manifest)
            self.assertEqual(2, result.returncode)
            self.assertIn("only supports wire_api='responses'", result.stderr)

    def test_custom_manifest_rejects_drifted_existing_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            manifest = PROJECT_ROOT / "config" / "deepseek-anthropic-1m.example.json"
            first = run_manifest_install(codex_home, manifest)
            self.assertEqual(0, first.returncode, first.stderr)
            config = codex_home / "config.toml"
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "http://127.0.0.1:18766",
                    "http://127.0.0.1:18767",
                ),
                encoding="utf-8",
            )

            second = run_manifest_install(codex_home, manifest)

            self.assertEqual(2, second.returncode)
            self.assertIn("differs from the manifest", second.stderr)

    def test_custom_manifest_doctor_checks_all_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            manifest = PROJECT_ROOT / "config" / "model-providers.example.json"
            installed = run_manifest_install(codex_home, manifest)
            self.assertEqual(0, installed.returncode, installed.stderr)
            result = subprocess.run(
                [
                    sys.executable,
                    str(DOCTOR_SCRIPT),
                    "--codex-home",
                    str(codex_home),
                    "--manifest",
                    str(manifest),
                    "--skip-keychain",
                    "--skip-adapter-health",
                    "--skip-codex",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("PASS  provider aicodemirror_claude", result.stdout)
            self.assertIn("PASS  provider claudecode_gemini", result.stdout)
            self.assertIn("PASS  wire API: responses", result.stdout)

    def test_custom_manifest_doctor_rejects_agent_binding_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            manifest = PROJECT_ROOT / "config" / "model-providers.example.json"
            installed = run_manifest_install(codex_home, manifest)
            self.assertEqual(0, installed.returncode, installed.stderr)
            agent = codex_home / "agents" / "aicodemirror_claude_worker.toml"
            agent.write_text(
                agent.read_text(encoding="utf-8").replace(
                    'model_provider = "aicodemirror_claude"',
                    'model_provider = "aicodemirror_gemini"',
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(DOCTOR_SCRIPT),
                    "--codex-home",
                    str(codex_home),
                    "--manifest",
                    str(manifest),
                    "--skip-keychain",
                    "--skip-adapter-health",
                    "--skip-codex",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(1, result.returncode)
            self.assertIn("FAIL  agent claude-opus-4-6", result.stdout)

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
            self.assertIn(install.toml_path_escape(str(codex_home.resolve())), agent)
            real_headers = [
                line for line in config.splitlines() if line.strip() == "[model_providers.deepseek]"
            ]
            self.assertEqual(1, len(real_headers))
            self.assertTrue((codex_home / "models" / "deepseek-v4-flash.json").is_file())
            self.assertTrue((codex_home / "skills" / "codex-custom-subagents" / "SKILL.md").is_file())

    def test_codex_home_falls_back_when_env_is_blank(self) -> None:
        expected_home = Path.home() / ".codex"
        with patch.dict(os.environ, {"CODEX_HOME": "   "}):
            self.assertEqual(expected_home, install.resolve_codex_home())
        with patch.dict(os.environ, {"CODEX_HOME": ""}):
            self.assertEqual(expected_home, install.resolve_codex_home())
        with patch.dict(os.environ, {}, clear=True), patch.object(
            install.Path, "home", return_value=expected_home.parent
        ):
            self.assertEqual(expected_home, install.resolve_codex_home())
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
            create_symlink_or_skip(
                self,
                codex_home / "skills" / "codex-custom-subagents",
                outside,
                target_is_directory=True,
            )

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
            create_symlink_or_skip(self, codex_home / "config.toml", outside_config)

            result = run_install(codex_home)

            self.assertEqual(2, result.returncode)
            self.assertIn("symlink", result.stderr)
            self.assertEqual("original", outside_config.read_text(encoding="utf-8"))

    def test_atomic_write_replaces_file_and_keeps_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "config.toml"
            destination.write_bytes(b"old content")

            install.install_content(b"new content", destination)

            self.assertEqual(b"new content", destination.read_bytes())
            backups = list(destination.parent.glob("config.toml.bak.*"))
            self.assertEqual(1, len(backups))
            self.assertEqual(b"old content", backups[0].read_bytes())

    def test_atomic_write_failure_leaves_original_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "config.toml"
            destination.write_bytes(b"old content")

            with patch.object(install.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    install.install_content(b"new content", destination)

            self.assertEqual(b"old content", destination.read_bytes())
            backups = list(destination.parent.glob("config.toml.bak.*"))
            self.assertEqual(1, len(backups))
            self.assertEqual(b"old content", backups[0].read_bytes())
            self.assertEqual([], list(destination.parent.glob(".config.toml.*.tmp")))

    def test_skill_upgrade_removes_only_manifested_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            first = run_install(codex_home)
            self.assertEqual(0, first.returncode, first.stderr)
            skill_dest = codex_home / "skills" / "codex-custom-subagents"
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
                [
                    sys.executable,
                    str(DOCTOR_SCRIPT),
                    "--codex-home",
                    str(codex_home),
                    "--skip-keychain",
                    "--skip-codex",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
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
                [
                    sys.executable,
                    str(DOCTOR_SCRIPT),
                    "--codex-home",
                    str(codex_home),
                    "--skip-keychain",
                    "--skip-codex",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertIn("FAIL  provider", result.stdout)
            self.assertIn("FAIL  provider auth", result.stdout)


if __name__ == "__main__":
    unittest.main()
