from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLAIM_SCRIPT = PROJECT_ROOT / "skills" / "codex-custom-subagents" / "scripts" / "claim_task.py"
HEADER = "# DeepSeek task handoff v1"


def mailbox(workspace: Path) -> Path:
    return workspace / ".deepseek-delegations"


def write_task(workspace: Path, task_id: str, declared_id: str | None = None) -> None:
    pending = mailbox(workspace) / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    declared = declared_id or task_id
    (pending / f"{task_id}.md").write_text(
        f"{HEADER}\n\nTask: {declared}\n\nReturn {task_id}.\n",
        encoding="utf-8",
    )


def run(
    workspace: Path,
    *args: str,
    cwd: Path | None = None,
    allow_workspace_mismatch: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(CLAIM_SCRIPT), "--workspace", str(workspace)]
    if allow_workspace_mismatch:
        command.append("--allow-workspace-mismatch")
    command.extend(args)
    return subprocess.run(
        command,
        cwd=cwd or workspace,
        text=True,
        capture_output=True,
        check=False,
    )


def claim(workspace: Path) -> subprocess.CompletedProcess[str]:
    return run(workspace)


def claimed_files(workspace: Path) -> list[Path]:
    return sorted((mailbox(workspace) / "claimed").glob("*.md"))


class ClaimTaskTests(unittest.TestCase):
    def test_parallel_workers_claim_unique_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            task_ids = ["task_alpha", "task_beta", "task_gamma", "task_delta"]
            for task_id in task_ids:
                write_task(workspace, task_id)

            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(lambda _: claim(workspace), task_ids))

            payloads = [json.loads(result.stdout) for result in results]
            self.assertTrue(all(result.returncode == 0 for result in results))
            self.assertEqual(set(task_ids), {payload["task_id"] for payload in payloads})
            self.assertEqual([], list((mailbox(workspace) / "pending").glob("*.md")))
            self.assertEqual(4, len(claimed_files(workspace)))

    def test_invalid_task_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_alpha", declared_id="wrong_id")

            result = claim(workspace)

            self.assertEqual(2, result.returncode)
            self.assertEqual("empty", json.loads(result.stdout)["status"])
            self.assertEqual(1, len(list((mailbox(workspace) / "rejected").glob("*.md"))))

    def test_empty_pool_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = claim(Path(directory))
            self.assertEqual(2, result.returncode)
            self.assertEqual("empty", json.loads(result.stdout)["status"])

    def test_empty_pool_leaves_no_garbage_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            result = claim(workspace)
            self.assertEqual(2, result.returncode)
            self.assertFalse((mailbox(workspace)).exists())

            workspace.joinpath(".deepseek-delegations", "pending").mkdir(parents=True)
            result = claim(workspace)
            self.assertEqual(2, result.returncode)
            self.assertEqual(
                {".deepseek-delegations", ".deepseek-delegations/pending"},
                {str(path.relative_to(workspace)) for path in workspace.rglob("*") if path.is_dir()},
            )

    def test_body_task_lines_do_not_falsely_reject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            pending = mailbox(workspace) / "pending"
            pending.mkdir(parents=True)
            (pending / "task_alpha.md").write_text(
                f"{HEADER}\n\nTask: task_alpha\n\nWork on /abs/path/project.\nTask: task_beta\nTask: task_alpha\n",
                encoding="utf-8",
            )

            result = claim(workspace)

            self.assertEqual(0, result.returncode)
            self.assertEqual("claimed", json.loads(result.stdout)["status"])
            self.assertEqual([], list((mailbox(workspace) / "rejected").glob("*.md")))

    def test_directories_symlinks_and_invalid_ids_are_skipped_with_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            pending = mailbox(workspace) / "pending"
            pending.mkdir(parents=True)
            write_task(workspace, "task_alpha")
            (pending / "dir.md").mkdir()
            (pending / "link.md").symlink_to(pending / "task_alpha.md")
            (pending / "Bad_Task.md").write_text(f"{HEADER}\n\nTask: Bad_Task\n", encoding="utf-8")

            result = claim(workspace)

            self.assertEqual(0, result.returncode)
            self.assertEqual("task_alpha", json.loads(result.stdout)["task_id"])
            self.assertEqual(["invalid_task_id", "not_a_file", "symlink"], sorted(json.loads(line)["reason"] for line in result.stderr.splitlines() if '"skipped"' in line))
            self.assertEqual(1, len(claimed_files(workspace)))
            self.assertTrue(claimed_files(workspace)[0].name.startswith("task_alpha--"))

    def test_claim_writes_durable_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_alpha")

            result = claim(workspace)
            payload = json.loads(result.stdout)

            self.assertEqual(0, result.returncode)
            claimed_path = Path(payload["path"])
            self.assertTrue(claimed_path.is_file())
            receipt = Path(payload["receipt"])
            self.assertTrue(receipt.is_file())
            self.assertEqual(payload, json.loads(receipt.read_text(encoding="utf-8")))
            self.assertEqual(1, payload["schema_version"])
            self.assertRegex(payload["attempt_id"], r"^[0-9a-f]{32}$")
            self.assertTrue(payload["claimed_at"].endswith("Z"))
            self.assertIsNone(payload["completed_at"])
            self.assertIsNone(payload["exit_code"])
            self.assertIsNone(payload["summary"])
            for field in ("parent_thread_id", "worker_thread_id", "agent", "model", "provider"):
                self.assertIsNone(payload[field])

    def test_claim_records_only_explicit_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_alpha")

            result = run(
                workspace,
                "--parent-thread-id",
                "thread-parent",
                "--worker-thread-id",
                "thread-worker",
                "--agent",
                "model_worker",
                "--model",
                "deepseek-v4",
                "--provider",
                "example-provider",
            )
            payload = json.loads(result.stdout)

            self.assertEqual(0, result.returncode)
            self.assertEqual("thread-parent", payload["parent_thread_id"])
            self.assertEqual("thread-worker", payload["worker_thread_id"])
            self.assertEqual("model_worker", payload["agent"])
            self.assertEqual("deepseek-v4", payload["model"])
            self.assertEqual("example-provider", payload["provider"])

    def test_claim_error_on_blocked_directories_returns_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_alpha")
            (mailbox(workspace) / "claimed").write_text("blocked", encoding="utf-8")

            result = claim(workspace)

            self.assertEqual(1, result.returncode)
            self.assertEqual("error", json.loads(result.stdout)["status"])
            self.assertNotIn("Traceback", result.stderr)

    def test_rejected_move_failure_returns_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_alpha", declared_id="wrong_id")
            (mailbox(workspace) / "rejected").write_text("blocked", encoding="utf-8")

            result = claim(workspace)

            self.assertEqual(1, result.returncode)
            self.assertEqual("error", json.loads(result.stdout)["status"])
            self.assertNotIn("Traceback", result.stderr)


class WorkspaceBoundaryTests(unittest.TestCase):
    def test_workspace_mismatch_is_rejected_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_alpha")

            result = run(workspace, cwd=PROJECT_ROOT)
            payload = json.loads(result.stdout)

            self.assertEqual(1, result.returncode)
            self.assertEqual("error", payload["status"])
            self.assertIn("workspace differs", payload["reason"])
            self.assertEqual(1, len(list((mailbox(workspace) / "pending").glob("*.md"))))
            self.assertFalse((mailbox(workspace) / "claimed").exists())

    def test_workspace_mismatch_requires_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_alpha")

            result = run(
                workspace,
                cwd=PROJECT_ROOT,
                allow_workspace_mismatch=True,
            )

            self.assertEqual(0, result.returncode)
            self.assertEqual("claimed", json.loads(result.stdout)["status"])
            self.assertIn("explicitly allowed", result.stderr)


class FinalizeTests(unittest.TestCase):
    def test_complete_atomically_updates_and_retains_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_alpha")
            claimed = json.loads(claim(workspace).stdout)

            result = run(
                workspace,
                "complete",
                "--task-id",
                "task_alpha",
                "--claim-id",
                claimed["claim_id"],
                "--summary",
                "tests passed",
            )
            payload = json.loads(result.stdout)
            receipt = Path(claimed["receipt"])

            self.assertEqual(0, result.returncode)
            self.assertEqual("completed", payload["status"])
            self.assertEqual(0, payload["exit_code"])
            self.assertEqual("tests passed", payload["summary"])
            self.assertTrue(payload["completed_at"].endswith("Z"))
            self.assertEqual(claimed["attempt_id"], payload["attempt_id"])
            self.assertTrue(Path(claimed["path"]).is_file())
            self.assertTrue(receipt.is_file())
            self.assertEqual(payload, json.loads(receipt.read_text(encoding="utf-8")))

            repeated = json.loads(run(
                workspace,
                "complete",
                "--task-id",
                "task_alpha",
                "--claim-id",
                claimed["claim_id"],
            ).stdout)
            self.assertEqual(payload, repeated)

    def test_fail_records_original_exit_code_without_deleting_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_alpha")
            claimed = json.loads(claim(workspace).stdout)

            result = run(
                workspace,
                "fail",
                "--task-id",
                "task_alpha",
                "--claim-id",
                claimed["claim_id"],
                "--exit-code",
                "17",
                "--summary",
                "unit test failed",
            )
            payload = json.loads(result.stdout)

            self.assertEqual(0, result.returncode)
            self.assertEqual("failed", payload["status"])
            self.assertEqual(17, payload["exit_code"])
            self.assertEqual("unit test failed", payload["summary"])
            self.assertTrue(Path(claimed["receipt"]).is_file())

    def test_complete_and_fail_cannot_overwrite_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_alpha")
            claimed = json.loads(claim(workspace).stdout)
            common = ("--task-id", "task_alpha", "--claim-id", claimed["claim_id"])

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(
                    lambda command: run(workspace, command, *common),
                    ("complete", "fail"),
                ))

            self.assertEqual([0, 1], sorted(result.returncode for result in results))
            receipt = json.loads(Path(claimed["receipt"]).read_text(encoding="utf-8"))
            self.assertIn(receipt["status"], {"completed", "failed"})
            self.assertEqual(claimed["attempt_id"], receipt["attempt_id"])

    def test_finalize_requires_exact_task_and_claim_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_alpha")
            claimed = json.loads(claim(workspace).stdout)

            result = run(
                workspace,
                "complete",
                "--task-id",
                "task_alpha",
                "--claim-id",
                "different-claim",
            )

            self.assertEqual(1, result.returncode)
            self.assertEqual("not_found", json.loads(result.stdout)["status"])
            self.assertEqual("claimed", json.loads(Path(claimed["receipt"]).read_text(encoding="utf-8"))["status"])


class RecoverTests(unittest.TestCase):
    def test_receipted_claims_need_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_alpha")
            claimed_payload = json.loads(claim(workspace).stdout)
            receipt = Path(claimed_payload["receipt"])
            self.assertTrue(receipt.is_file())

            result = run(workspace, "recover")
            payload = json.loads(result.stdout)
            self.assertEqual(0, result.returncode)
            self.assertEqual([], payload["requeued"])
            self.assertEqual("running_or_unacknowledged", payload["skipped"][0]["reason"])
            self.assertEqual(1, len(claimed_files(workspace)))

            result = run(workspace, "recover", "--task-id", "task_alpha")
            payload = json.loads(result.stdout)
            self.assertEqual(0, result.returncode)
            self.assertEqual(1, len(payload["requeued"]))
            self.assertEqual([], claimed_files(workspace))
            self.assertEqual(1, len(list((mailbox(workspace) / "recovered").glob("*.md"))))
            self.assertTrue(receipt.exists())
            self.assertEqual("recovered", json.loads(receipt.read_text(encoding="utf-8"))["status"])

    def test_recover_to_pending_retains_attempt_audit_and_next_claim_is_new_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_alpha")
            first = json.loads(claim(workspace).stdout)

            recovered = run(
                workspace,
                "recover",
                "--task-id",
                "task_alpha",
                "--claim-id",
                first["claim_id"],
                "--to",
                "pending",
            )
            first_receipt = Path(first["receipt"])

            self.assertEqual(0, recovered.returncode)
            self.assertTrue(first_receipt.is_file())
            first_audit = json.loads(first_receipt.read_text(encoding="utf-8"))
            self.assertEqual("recovered", first_audit["status"])
            self.assertEqual("pending", first_audit["recovery_target"])
            self.assertTrue((mailbox(workspace) / "pending" / "task_alpha.md").is_file())

            second = json.loads(claim(workspace).stdout)
            self.assertNotEqual(first["claim_id"], second["claim_id"])
            self.assertNotEqual(first["attempt_id"], second["attempt_id"])
            self.assertTrue(first_receipt.is_file())
            self.assertTrue(Path(second["receipt"]).is_file())

    def test_missing_receipt_claims_need_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            claimed_dir = mailbox(workspace) / "claimed"
            claimed_dir.mkdir(parents=True)
            claimed_file = claimed_dir / "task_gamma--c2.md"
            claimed_file.write_text(f"{HEADER}\n\nTask: task_gamma\n", encoding="utf-8")

            payload = json.loads(run(workspace, "recover").stdout)
            self.assertEqual("missing_receipt", payload["skipped"][0]["reason"])

            payload = json.loads(run(workspace, "recover", "--task-id", "task_gamma").stdout)
            self.assertEqual(1, len(payload["requeued"]))
            self.assertTrue((mailbox(workspace) / "recovered" / "task_gamma--c2.md").is_file())

    def test_orphan_receipts_are_audited_and_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_delta")
            claimed = json.loads(claim(workspace).stdout)
            claimed_path = Path(claimed["path"])
            orphan = Path(claimed["receipt"])
            claimed_path.replace(mailbox(workspace) / "pending" / "task_delta.md")

            payload = json.loads(run(workspace, "recover").stdout)

            self.assertEqual("retained_orphan_receipt", payload["audited"][0]["reason"])
            self.assertTrue(orphan.exists())
            self.assertEqual("orphaned", json.loads(orphan.read_text(encoding="utf-8"))["status"])

    def test_invalid_claimed_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            claimed_dir = mailbox(workspace) / "claimed"
            claimed_dir.mkdir(parents=True)
            (claimed_dir / "task_epsilon--c4.md").write_text("not a task file", encoding="utf-8")

            payload = json.loads(run(workspace, "recover").stdout)

            self.assertEqual(1, len(payload["rejected"]))
            self.assertEqual([], claimed_files(workspace))
            self.assertEqual(1, len(list((mailbox(workspace) / "rejected").glob("*.md"))))

    def test_recover_all_to_pending_and_collision_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_alpha")
            claim(workspace)

            payload = json.loads(run(workspace, "recover", "--all", "--to", "pending").stdout)
            self.assertEqual(1, len(payload["requeued"]))
            self.assertTrue((mailbox(workspace) / "pending" / "task_alpha.md").is_file())

            claimed_dir = mailbox(workspace) / "claimed"
            claimed_dir.mkdir(parents=True, exist_ok=True)
            (claimed_dir / "task_alpha--c5.md").write_text(f"{HEADER}\n\nTask: task_alpha\n", encoding="utf-8")
            (claimed_dir / "task_alpha--c5.md.receipt").write_text("{}", encoding="utf-8")

            payload = json.loads(run(workspace, "recover", "--task-id", "task_alpha", "--to", "pending").stdout)
            self.assertEqual(1, len(payload["failed"]))
            self.assertIn("already exists", payload["failed"][0]["reason"])
            self.assertEqual(1, len(claimed_files(workspace)))

    def test_recover_dry_run_moves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_alpha")
            claimed_path = Path(json.loads(claim(workspace).stdout)["path"])

            payload = json.loads(run(workspace, "recover", "--all", "--dry-run").stdout)

            self.assertTrue(payload["dry_run"])
            self.assertEqual("would_requeue", payload["requeued"][0]["action"])
            self.assertTrue(claimed_path.is_file())
            self.assertFalse((mailbox(workspace) / "recovered").exists())

    def test_recover_empty_pool_creates_no_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            payload = json.loads(run(workspace, "recover").stdout)
            self.assertEqual("recovered", payload["status"])
            self.assertFalse((mailbox(workspace)).exists())


class LocateTests(unittest.TestCase):
    def test_locate_exact_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_alpha")
            claimed_payload = json.loads(claim(workspace).stdout)

            payload = json.loads(run(workspace, "locate", "--task-id", "task_alpha").stdout)
            self.assertEqual("located", payload["status"])
            self.assertEqual(claimed_payload["claim_id"], payload["claim_id"])
            self.assertEqual(claimed_payload["path"], payload["path"])

    def test_locate_with_claim_id_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_task(workspace, "task_alpha")
            claimed_payload = json.loads(claim(workspace).stdout)
            claimed_dir = mailbox(workspace) / "claimed"
            (claimed_dir / "task_alpha--c6.md").write_text(f"{HEADER}\n\nTask: task_alpha\n", encoding="utf-8")

            ambiguous = run(workspace, "locate", "--task-id", "task_alpha")
            self.assertEqual(3, ambiguous.returncode)
            self.assertEqual("ambiguous", json.loads(ambiguous.stdout)["status"])

            exact = run(workspace, "locate", "--task-id", "task_alpha", "--claim-id", claimed_payload["claim_id"])
            self.assertEqual(0, exact.returncode)
            self.assertEqual(claimed_payload["path"], json.loads(exact.stdout)["path"])

    def test_locate_missing_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run(Path(directory), "locate", "--task-id", "task_nope")
            self.assertEqual(1, result.returncode)
            self.assertEqual("not_found", json.loads(result.stdout)["status"])


if __name__ == "__main__":
    unittest.main()
