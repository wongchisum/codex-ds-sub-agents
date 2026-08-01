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
CLAIM_SCRIPT = PROJECT_ROOT / "skills" / "deepseek-delegation" / "scripts" / "claim_task.py"
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


def run(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLAIM_SCRIPT), "--workspace", str(workspace), *args],
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
            self.assertFalse(receipt.exists())

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

    def test_orphan_receipts_are_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            claimed_dir = mailbox(workspace) / "claimed"
            claimed_dir.mkdir(parents=True)
            orphan = claimed_dir / "task_delta--c3.md.receipt"
            orphan.write_text('{"status": "claimed"}', encoding="utf-8")

            payload = json.loads(run(workspace, "recover").stdout)

            self.assertEqual("orphan_receipt", payload["cleaned"][0]["reason"])
            self.assertFalse(orphan.exists())

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
