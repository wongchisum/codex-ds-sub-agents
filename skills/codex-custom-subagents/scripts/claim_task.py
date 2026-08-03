#!/usr/bin/env python3
"""Atomic claim and recovery for a Codex Custom Subagents task pool.

The pool lives at the calling thread's REAL cwd (``.codex-custom-subagents``):
relative ``--workspace`` values are resolved against the process cwd and all
symlinks are resolved, so threads sharing a logical path share one pool. The
project a task operates on is independent of the pool and is given as an
absolute path inside the task body.

Exit codes: 0 success, 1 operational error, 2 empty pool, 3 ambiguous locate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from platform_lock import PlatformLockError, platform_file_lock


TASK_ID_PATTERN = re.compile(r"[a-z0-9_]{1,64}")
CLAIM_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
MAILBOX_NAME = ".codex-custom-subagents"
LEGACY_MAILBOX_NAME = ".deepseek-delegations"
HEADER = "# Codex Custom Subagents task handoff v1"
LEGACY_HEADER = "# DeepSeek task handoff v1"
SUPPORTED_HEADERS = frozenset({HEADER, LEGACY_HEADER})
RECEIPT_SCHEMA_VERSION = 1
TERMINAL_STATUSES = frozenset({"completed", "failed", "recovered", "rejected", "orphaned"})
METADATA_FIELDS = ("parent_thread_id", "worker_thread_id", "agent", "model", "provider")
# Protocol header block: header line, blank line, "Task: <id>" line, blank line.
# The validator never inspects the task body, so body lines starting with
# "Task: " cannot cause a false rejection.
METADATA_LINES = 3


class Pool:
    def __init__(self, workspace: Path, *, legacy_mailbox: bool = False) -> None:
        self.legacy_mailbox = legacy_mailbox
        mailbox = workspace / (LEGACY_MAILBOX_NAME if legacy_mailbox else MAILBOX_NAME)
        self.mailbox = mailbox
        self.lock = mailbox / ".lock"
        self.pending = mailbox / "pending"
        self.claimed = mailbox / "claimed"
        self.rejected = mailbox / "rejected"
        self.recovered = mailbox / "recovered"


def emit(payload: dict[str, Any], *, to_stderr: bool = False) -> None:
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr if to_stderr else sys.stdout)


def resolve_workspace(raw: str) -> Path:
    """Resolve the pool root against the calling process's real cwd."""
    return Path(raw).expanduser().resolve()


def new_claim_id() -> str:
    return f"{os.getpid()}-{uuid.uuid4().hex[:12]}"


def new_attempt_id() -> str:
    return uuid.uuid4().hex


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@contextmanager
def lock_pool(pool: Pool) -> Iterator[None]:
    """Serialize lifecycle transitions while keeping file moves atomic."""
    pool.mailbox.mkdir(parents=True, exist_ok=True)
    try:
        with platform_file_lock(pool.lock, exclusive=True):
            yield
    except PlatformLockError as error:
        raise OSError(str(error)) from error


def split_claim_name(name: str) -> tuple[str, str] | None:
    """Parse ``<task_id>--<claim_id>.md``; return None when the name is invalid."""
    if not name.endswith(".md"):
        return None
    stem = name[: -len(".md")]
    task_id, separator, claim_id = stem.rpartition("--")
    if not separator or not task_id or not claim_id:
        return None
    if not TASK_ID_PATTERN.fullmatch(task_id) or not CLAIM_ID_PATTERN.fullmatch(claim_id):
        return None
    return task_id, claim_id


def validate_task(
    path: Path,
    task_id: str,
    *,
    allow_legacy_header: bool = False,
) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        return f"cannot read task: {error}"

    supported_headers = SUPPORTED_HEADERS if allow_legacy_header else frozenset({HEADER})
    if not lines or lines[0] not in supported_headers:
        return f"first line must be {HEADER!r}"

    metadata = lines[1 : 1 + METADATA_LINES]
    task_lines = [line for line in metadata if line.startswith("Task: ")]
    if task_lines != [f"Task: {task_id}"]:
        return "Task metadata line must appear exactly once in the header block and match the filename"
    return None


def write_receipt(receipt: Path, payload: dict[str, Any]) -> None:
    """Atomically persist and flush a lifecycle record."""
    temp = receipt.with_name(f".{receipt.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        with temp.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, receipt)
    except OSError:
        temp.unlink(missing_ok=True)
        raise


def remove_receipt(receipt: Path) -> None:
    """Remove a receipt created for a claim rename that never succeeded."""
    try:
        receipt.unlink(missing_ok=True)
    except OSError:
        pass


def read_receipt(receipt: Path) -> tuple[dict[str, Any] | None, str | None]:
    if receipt.is_symlink() or not receipt.is_file():
        return None, "receipt is missing or not a regular file"
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return None, f"cannot read receipt: {error}"
    if not isinstance(payload, dict):
        return None, "receipt must contain a JSON object"
    return payload, None


def validate_receipt_identity(payload: dict[str, Any], task_id: str, claim_id: str) -> str | None:
    if payload.get("task_id") != task_id or payload.get("claim_id") != claim_id:
        return "receipt identity does not match task_id and claim_id"
    if payload.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        return f"unsupported receipt schema_version: {payload.get('schema_version')!r}"
    if not isinstance(payload.get("attempt_id"), str) or not payload["attempt_id"]:
        return "receipt attempt_id is missing"
    return None


def claim_payload(
    task_id: str,
    claim_id: str,
    attempt_id: str,
    destination: Path,
    receipt: Path,
    metadata: dict[str, str | None],
) -> dict[str, Any]:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "claimed",
        "task_id": task_id,
        "claim_id": claim_id,
        "attempt_id": attempt_id,
        "claimed_at": utc_now(),
        "completed_at": None,
        "exit_code": None,
        "summary": None,
        "path": str(destination),
        "receipt": str(receipt),
        **{field: metadata.get(field) for field in METADATA_FIELDS},
    }


def claim_one(pool: Pool, metadata: dict[str, str | None]) -> int:
    if not pool.pending.is_dir():
        emit({"status": "empty", "reason": "pending directory does not exist"})
        return 2
    try:
        with lock_pool(pool):
            return _claim_one_locked(pool, metadata)
    except OSError as error:
        emit({"status": "error", "reason": f"cannot lock task pool: {error}"})
        return 1


def _claim_one_locked(pool: Pool, metadata: dict[str, str | None]) -> int:
    pending = pool.pending
    if not pending.is_dir():
        emit({"status": "empty", "reason": "pending directory does not exist"})
        return 2

    try:
        candidates = sorted(pending.iterdir(), key=lambda item: item.name)
    except OSError as error:
        emit({"status": "error", "reason": f"cannot scan pending: {error}"})
        return 1

    for candidate in candidates:
        if not candidate.name.endswith(".md"):
            continue  # receipts, temp files, and other non-task entries
        if candidate.is_symlink():
            emit({"status": "skipped", "reason": "symlink", "path": str(candidate)}, to_stderr=True)
            continue
        if not candidate.is_file():
            emit({"status": "skipped", "reason": "not_a_file", "path": str(candidate)}, to_stderr=True)
            continue

        task_id = candidate.name[: -len(".md")]
        if not TASK_ID_PATTERN.fullmatch(task_id):
            emit({"status": "skipped", "reason": "invalid_task_id", "task_id": task_id, "path": str(candidate)}, to_stderr=True)
            continue

        validation_error = validate_task(
            candidate,
            task_id,
            allow_legacy_header=pool.legacy_mailbox,
        )
        if validation_error is not None:
            rejected_path = pool.rejected / f"{task_id}--{new_claim_id()}.md"
            try:
                pool.rejected.mkdir(parents=True, exist_ok=True)
                candidate.replace(rejected_path)
            except FileNotFoundError:
                continue  # another worker rejected this task first
            except OSError as error:
                emit({"status": "error", "reason": f"cannot move rejected task: {error}", "task_id": task_id})
                return 1
            emit({"status": "rejected", "task_id": task_id, "path": str(rejected_path), "reason": validation_error}, to_stderr=True)
            continue

        claim_id = new_claim_id()
        attempt_id = new_attempt_id()
        destination = pool.claimed / f"{task_id}--{claim_id}.md"
        receipt = pool.claimed / f"{destination.name}.receipt"
        payload = claim_payload(task_id, claim_id, attempt_id, destination, receipt, metadata)
        try:
            # The durable receipt is written BEFORE the atomic rename, so a
            # crash after the rename but before stdout leaves a fully recorded
            # claim that `recover` can classify without guessing.
            pool.claimed.mkdir(parents=True, exist_ok=True)
            write_receipt(receipt, payload)
            candidate.replace(destination)
        except FileNotFoundError:
            remove_receipt(receipt)
            continue  # another worker claimed this task first
        except OSError as error:
            remove_receipt(receipt)
            emit({"status": "error", "reason": str(error), "task_id": task_id})
            return 1

        emit(payload)
        return 0

    emit({"status": "empty", "reason": "no valid pending task"})
    return 2


def finalize_claim(
    pool: Pool,
    task_id: str,
    claim_id: str,
    status: str,
    exit_code: int | None,
    summary: str | None,
) -> int:
    """Atomically mark one exact claim completed or failed."""
    claimed_path = pool.claimed / f"{task_id}--{claim_id}.md"
    receipt = pool.claimed / f"{claimed_path.name}.receipt"
    if not pool.claimed.is_dir():
        emit({"status": "not_found", "task_id": task_id, "claim_id": claim_id})
        return 1

    try:
        with lock_pool(pool):
            if claimed_path.is_symlink() or not claimed_path.is_file():
                emit({"status": "not_found", "task_id": task_id, "claim_id": claim_id})
                return 1

            payload, read_error = read_receipt(receipt)
            if read_error is not None or payload is None:
                emit({
                    "status": "error",
                    "task_id": task_id,
                    "claim_id": claim_id,
                    "reason": read_error,
                })
                return 1
            identity_error = validate_receipt_identity(payload, task_id, claim_id)
            if identity_error is not None:
                emit({
                    "status": "error",
                    "task_id": task_id,
                    "claim_id": claim_id,
                    "reason": identity_error,
                })
                return 1

            current_status = payload.get("status")
            if current_status == status:
                emit(payload)
                return 0
            if current_status != "claimed":
                emit({
                    "status": "error",
                    "task_id": task_id,
                    "claim_id": claim_id,
                    "reason": f"claim already has terminal status {current_status!r}",
                })
                return 1

            completed = {
                **payload,
                "status": status,
                "completed_at": utc_now(),
                "exit_code": exit_code,
                "summary": summary,
            }
            write_receipt(receipt, completed)
            emit(completed)
            return 0
    except OSError as error:
        emit({
            "status": "error",
            "task_id": task_id,
            "claim_id": claim_id,
            "reason": f"cannot finalize claim: {error}",
        })
        return 1


def requeue_destination(target: Path, pool: Pool, task_id: str, claim_id: str) -> Path | None:
    """Return the deterministic requeue destination, or None on collision."""
    if target == pool.recovered:
        return target / f"{task_id}--{claim_id}.md"
    destination = target / f"{task_id}.md"
    if destination.exists():
        return None
    return destination


def recover(pool: Pool, task_ids: set[str], claim_ids: set[str], all_claims: bool, dry_run: bool, to: Path) -> int:
    """Move confirmed claims while retaining every successful attempt receipt."""
    if not pool.claimed.is_dir():
        emit({
            "status": "recovered",
            "dry_run": dry_run,
            "requeued": [],
            "rejected": [],
            "audited": [],
            "cleaned": [],
            "skipped": [],
            "failed": [],
        })
        return 0
    try:
        with lock_pool(pool):
            return _recover_locked(pool, task_ids, claim_ids, all_claims, dry_run, to)
    except OSError as error:
        emit({"status": "error", "reason": f"cannot lock task pool: {error}"})
        return 1


def _recover_locked(
    pool: Pool,
    task_ids: set[str],
    claim_ids: set[str],
    all_claims: bool,
    dry_run: bool,
    to: Path,
) -> int:
    requeued: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    audited: list[dict[str, Any]] = []
    cleaned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    try:
        claimed_files = sorted(pool.claimed.iterdir(), key=lambda item: item.name)
    except OSError as error:
        emit({"status": "error", "reason": f"cannot scan claimed: {error}"})
        return 1

    for claimed_file in claimed_files:
        if not claimed_file.name.endswith(".md"):
            continue
        if claimed_file.is_symlink() or not claimed_file.is_file():
            skipped.append({"path": str(claimed_file), "reason": "not_a_regular_file"})
            continue

        parsed = split_claim_name(claimed_file.name)
        if parsed is None:
            skipped.append({"path": str(claimed_file), "reason": "invalid_claim_name"})
            continue
        task_id, claim_id = parsed

        receipt = pool.claimed / f"{claimed_file.name}.receipt"
        receipt_payload, receipt_error = read_receipt(receipt)
        if receipt_payload is not None:
            receipt_error = validate_receipt_identity(receipt_payload, task_id, claim_id)

        validation_error = validate_task(
            claimed_file,
            task_id,
            allow_legacy_header=pool.legacy_mailbox,
        )
        if validation_error is not None:
            entry = {"task_id": task_id, "claim_id": claim_id, "path": str(claimed_file), "reason": validation_error}
            if dry_run:
                rejected.append({**entry, "action": "would_reject"})
                continue
            rejected_path = pool.rejected / f"{task_id}--{claim_id}.md"
            try:
                pool.rejected.mkdir(parents=True, exist_ok=True)
                claimed_file.replace(rejected_path)
                if receipt_payload is not None and receipt_error is None and receipt_payload.get("status") == "claimed":
                    write_receipt(receipt, {
                        **receipt_payload,
                        "status": "rejected",
                        "completed_at": utc_now(),
                        "rejected_path": str(rejected_path),
                        "rejection_reason": validation_error,
                    })
                rejected.append({**entry, "path": str(rejected_path), "receipt": str(receipt) if receipt.is_file() else None})
            except OSError as error:
                failed.append({**entry, "reason": f"cannot move to rejected: {error}"})
            continue

        receipt_status = receipt_payload.get("status") if receipt_payload is not None and receipt_error is None else None
        if receipt_status in TERMINAL_STATUSES:
            skipped.append({
                "task_id": task_id,
                "claim_id": claim_id,
                "path": str(claimed_file),
                "reason": f"terminal_{receipt_status}",
            })
            continue

        selected = all_claims or task_id in task_ids or claim_id in claim_ids or receipt_status == "recovering"
        if not selected:
            reason = "missing_receipt" if not receipt.is_file() else "running_or_unacknowledged"
            entry = {"task_id": task_id, "claim_id": claim_id, "path": str(claimed_file), "reason": reason}
            if receipt_error is not None and receipt.is_file():
                entry["receipt_error"] = receipt_error
            skipped.append(entry)
            continue

        destination = requeue_destination(to, pool, task_id, claim_id)
        if receipt_status == "recovering":
            recorded_destination = receipt_payload.get("recovered_path") if receipt_payload is not None else None
            valid_destinations = {
                str(pool.pending / f"{task_id}.md"): pool.pending / f"{task_id}.md",
                str(pool.recovered / f"{task_id}--{claim_id}.md"): pool.recovered / f"{task_id}--{claim_id}.md",
            }
            destination = valid_destinations.get(recorded_destination)
            if destination is None:
                failed.append({
                    "task_id": task_id,
                    "claim_id": claim_id,
                    "path": str(claimed_file),
                    "reason": "recovering receipt has an invalid recovered_path",
                })
                continue

        if destination is None:
            failed.append({"task_id": task_id, "claim_id": claim_id, "path": str(claimed_file), "reason": "requeue destination already exists"})
            continue
        if destination.exists():
            failed.append({"task_id": task_id, "claim_id": claim_id, "path": str(claimed_file), "reason": "requeue destination already exists"})
            continue
        if dry_run:
            requeued.append({"task_id": task_id, "claim_id": claim_id, "path": str(destination), "action": "would_requeue"})
            continue

        original_receipt = receipt_payload
        try:
            if receipt_payload is not None and receipt_error is None and receipt_status == "claimed":
                receipt_payload = {
                    **receipt_payload,
                    "status": "recovering",
                    "recovery_started_at": utc_now(),
                    "recovery_target": destination.parent.name,
                    "recovered_path": str(destination),
                }
                write_receipt(receipt, receipt_payload)
            destination.parent.mkdir(parents=True, exist_ok=True)
            claimed_file.replace(destination)
            if receipt_payload is not None and receipt_error is None:
                write_receipt(receipt, {
                    **receipt_payload,
                    "status": "recovered",
                    "completed_at": utc_now(),
                    "recovered_at": utc_now(),
                    "recovery_target": destination.parent.name,
                    "recovered_path": str(destination),
                })
            entry = {
                "task_id": task_id,
                "claim_id": claim_id,
                "path": str(destination),
                "receipt": str(receipt) if receipt.is_file() else None,
            }
            if receipt_error is not None and receipt.is_file():
                entry["receipt_error"] = receipt_error
            requeued.append(entry)
        except OSError as error:
            if claimed_file.exists() and original_receipt is not None and receipt_error is None:
                try:
                    write_receipt(receipt, original_receipt)
                except OSError:
                    pass
            failed.append({"task_id": task_id, "claim_id": claim_id, "path": str(claimed_file), "reason": f"cannot requeue: {error}"})

    try:
        receipts = sorted(pool.claimed.glob("*.receipt"), key=lambda item: item.name)
    except OSError as error:
        emit({"status": "error", "reason": f"cannot scan receipts: {error}"})
        return 1
    for receipt in receipts:
        claimed_file = pool.claimed / receipt.name[: -len(".receipt")]
        if claimed_file.exists():
            continue
        parsed = split_claim_name(claimed_file.name)
        payload, receipt_error = read_receipt(receipt)
        if parsed is None or payload is None:
            skipped.append({"path": str(receipt), "reason": "invalid_orphan_receipt", "receipt_error": receipt_error})
            continue
        task_id, claim_id = parsed
        identity_error = validate_receipt_identity(payload, task_id, claim_id)
        if identity_error is not None:
            skipped.append({"path": str(receipt), "reason": "invalid_orphan_receipt", "receipt_error": identity_error})
            continue

        receipt_status = payload.get("status")
        entry = {"task_id": task_id, "claim_id": claim_id, "path": str(receipt)}
        if receipt_status in TERMINAL_STATUSES:
            audited.append({**entry, "reason": f"retained_{receipt_status}_receipt"})
            continue
        if receipt_status == "recovering" and isinstance(payload.get("recovered_path"), str):
            recovered_path = Path(payload["recovered_path"])
            valid_paths = {
                pool.pending / f"{task_id}.md",
                pool.recovered / f"{task_id}--{claim_id}.md",
            }
            if recovered_path in valid_paths and recovered_path.is_file() and not recovered_path.is_symlink():
                if dry_run:
                    audited.append({**entry, "action": "would_finish_recovery"})
                else:
                    write_receipt(receipt, {
                        **payload,
                        "status": "recovered",
                        "completed_at": utc_now(),
                        "recovered_at": utc_now(),
                    })
                    audited.append({**entry, "reason": "finished_interrupted_recovery"})
                continue
        if receipt_status == "claimed":
            if dry_run:
                audited.append({**entry, "action": "would_mark_orphaned"})
            else:
                write_receipt(receipt, {
                    **payload,
                    "status": "orphaned",
                    "completed_at": utc_now(),
                })
                audited.append({**entry, "reason": "retained_orphan_receipt"})
            continue
        skipped.append({**entry, "reason": f"unsupported_orphan_status_{receipt_status}"})

    emit({
        "status": "recovered",
        "dry_run": dry_run,
        "requeued": requeued,
        "rejected": rejected,
        "audited": audited,
        "cleaned": cleaned,
        "skipped": skipped,
        "failed": failed,
    })
    return 1 if failed else 0


def locate(pool: Pool, task_id: str, claim_id: str | None) -> int:
    if claim_id is not None:
        path = pool.claimed / f"{task_id}--{claim_id}.md"
        if path.is_file() and not path.is_symlink():
            emit({"status": "located", "task_id": task_id, "claim_id": claim_id, "path": str(path)})
            return 0
        emit({"status": "not_found", "task_id": task_id, "claim_id": claim_id})
        return 1

    matches: list[Path] = []
    if pool.claimed.is_dir():
        try:
            candidates = sorted(pool.claimed.iterdir(), key=lambda item: item.name)
        except OSError as error:
            emit({"status": "error", "reason": f"cannot scan claimed: {error}"})
            return 1
        for candidate in candidates:
            if not candidate.name.endswith(".md") or candidate.is_symlink() or not candidate.is_file():
                continue
            if candidate.name[: -len(".md")].startswith(f"{task_id}--"):
                matches.append(candidate)

    if len(matches) == 1:
        match = matches[0]
        found_claim_id = match.name[: -len(".md")].rpartition("--")[2]
        emit({"status": "located", "task_id": task_id, "claim_id": found_claim_id, "path": str(match)})
        return 0
    if len(matches) > 1:
        emit({"status": "ambiguous", "task_id": task_id, "matches": [str(item) for item in matches]})
        return 3
    emit({"status": "not_found", "task_id": task_id})
    return 1


def task_id_arg(raw: str) -> str:
    if not TASK_ID_PATTERN.fullmatch(raw):
        raise argparse.ArgumentTypeError("must match [a-z0-9_]{1,64}")
    return raw


def claim_id_arg(raw: str) -> str:
    if not CLAIM_ID_PATTERN.fullmatch(raw):
        raise argparse.ArgumentTypeError("must contain only letters, digits, underscores, and hyphens (max 128 characters)")
    return raw


def metadata_arg(raw: str) -> str:
    if len(raw) > 512 or any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise argparse.ArgumentTypeError("must be at most 512 characters and contain no control characters")
    return raw


def summary_arg(raw: str) -> str:
    if len(raw) > 4096 or "\x00" in raw:
        raise argparse.ArgumentTypeError("must be at most 4096 characters and contain no NUL byte")
    return raw


def add_finalize_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-id", type=task_id_arg, required=True)
    parser.add_argument("--claim-id", type=claim_id_arg, required=True)
    parser.add_argument("--exit-code", type=int, default=None, help="original task/process exit code; omit when no real code exists")
    parser.add_argument("--summary", type=summary_arg, default=None, help="compact non-secret outcome summary")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Atomically claim and audit custom-subagent task attempts.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="pool root; defaults to the real cwd ('.codex-custom-subagents' inside it)",
    )
    parser.add_argument(
        "--legacy-mailbox",
        action="store_true",
        help="use the legacy .deepseek-delegations pool only to finish pre-upgrade work",
    )
    parser.add_argument(
        "--allow-workspace-mismatch",
        "--allow-non-cwd-workspace",
        action="store_true",
        help="explicitly allow --workspace to differ from the process's real cwd",
    )
    for field in METADATA_FIELDS:
        parser.add_argument(f"--{field.replace('_', '-')}", type=metadata_arg, default=None)
    subparsers = parser.add_subparsers(dest="command")

    recover_parser = subparsers.add_parser("recover", help="recover orphaned or parent-confirmed claimed tasks")
    recover_parser.add_argument("--task-id", type=task_id_arg, action="append", default=[], help="requeue this task (repeatable; parent confirms its worker is dead)")
    recover_parser.add_argument("--claim-id", type=claim_id_arg, action="append", default=[], help="requeue this exact claim (repeatable)")
    recover_parser.add_argument("--all", action="store_true", help="requeue every claimed task; only after confirming no workers are running")
    recover_parser.add_argument("--dry-run", action="store_true", help="classify and report without moving anything")
    recover_parser.add_argument("--to", choices=("recovered", "pending"), default="recovered", help="requeue destination directory")

    locate_parser = subparsers.add_parser("locate", help="deterministically locate a claimed task file")
    locate_parser.add_argument("--task-id", type=task_id_arg, required=True)
    locate_parser.add_argument("--claim-id", type=claim_id_arg, default=None)

    complete_parser = subparsers.add_parser("complete", help="atomically record successful completion for one exact claim")
    add_finalize_args(complete_parser)
    complete_parser.set_defaults(exit_code=0)

    fail_parser = subparsers.add_parser("fail", help="atomically record task failure for one exact claim")
    add_finalize_args(fail_parser)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        workspace = resolve_workspace(args.workspace)
    except OSError as error:
        emit({"status": "error", "reason": f"cannot resolve workspace: {error}"})
        return 1
    real_cwd = Path.cwd().resolve()
    if workspace != real_cwd and not args.allow_workspace_mismatch:
        emit({
            "status": "error",
            "reason": "workspace differs from the real cwd; pass --allow-workspace-mismatch only for deliberate compatibility use",
            "workspace": str(workspace),
            "real_cwd": str(real_cwd),
        })
        return 1
    if workspace != real_cwd:
        emit({
            "status": "warning",
            "reason": "workspace mismatch explicitly allowed; the canonical task pool still belongs at the calling thread's real cwd",
            "workspace": str(workspace),
            "real_cwd": str(real_cwd),
        }, to_stderr=True)

    pool = Pool(workspace, legacy_mailbox=args.legacy_mailbox)
    if args.command == "recover":
        target = pool.recovered if args.to == "recovered" else pool.pending
        return recover(pool, set(args.task_id), set(args.claim_id), args.all, args.dry_run, target)
    if args.command == "locate":
        return locate(pool, args.task_id, args.claim_id)
    if args.command == "complete":
        return finalize_claim(pool, args.task_id, args.claim_id, "completed", args.exit_code, args.summary)
    if args.command == "fail":
        return finalize_claim(pool, args.task_id, args.claim_id, "failed", args.exit_code, args.summary)
    metadata = {field: getattr(args, field) for field in METADATA_FIELDS}
    return claim_one(pool, metadata)


if __name__ == "__main__":
    raise SystemExit(main())
