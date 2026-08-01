#!/usr/bin/env python3
"""Atomic claim and recovery for a DeepSeek delegation task pool.

The pool lives at the calling thread's REAL cwd (``.deepseek-delegations``):
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
from pathlib import Path
from typing import Any


TASK_ID_PATTERN = re.compile(r"[a-z0-9_]{1,64}")
HEADER = "# DeepSeek task handoff v1"
# Protocol header block: header line, blank line, "Task: <id>" line, blank line.
# The validator never inspects the task body, so body lines starting with
# "Task: " cannot cause a false rejection.
METADATA_LINES = 3


class Pool:
    def __init__(self, workspace: Path) -> None:
        mailbox = workspace / ".deepseek-delegations"
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


def split_claim_name(name: str) -> tuple[str, str] | None:
    """Parse ``<task_id>--<claim_id>.md``; return None when the name is invalid."""
    if not name.endswith(".md"):
        return None
    stem = name[: -len(".md")]
    task_id, separator, claim_id = stem.rpartition("--")
    if not separator or not task_id or not claim_id:
        return None
    if not TASK_ID_PATTERN.fullmatch(task_id):
        return None
    return task_id, claim_id


def validate_task(path: Path, task_id: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        return f"cannot read task: {error}"

    if not lines or lines[0] != HEADER:
        return f"first line must be {HEADER!r}"

    metadata = lines[1 : 1 + METADATA_LINES]
    task_lines = [line for line in metadata if line.startswith("Task: ")]
    if task_lines != [f"Task: {task_id}"]:
        return "Task metadata line must appear exactly once in the header block and match the filename"
    return None


def write_receipt(receipt: Path, payload: dict[str, Any]) -> None:
    """Atomically persist the claim record before the worker touches stdout."""
    temp = receipt.with_name(f".{receipt.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp, receipt)
    except OSError:
        temp.unlink(missing_ok=True)
        raise


def remove_receipt(receipt: Path) -> None:
    """Best-effort receipt cleanup; failures never mask the claim result."""
    try:
        receipt.unlink(missing_ok=True)
    except OSError:
        pass


def claim_one(pool: Pool) -> int:
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

        validation_error = validate_task(candidate, task_id)
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
        destination = pool.claimed / f"{task_id}--{claim_id}.md"
        receipt = pool.claimed / f"{destination.name}.receipt"
        payload = {"status": "claimed", "task_id": task_id, "claim_id": claim_id, "path": str(destination), "receipt": str(receipt)}
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


def requeue_destination(target: Path, pool: Pool, task_id: str, claim_id: str) -> Path | None:
    """Return the deterministic requeue destination, or None on collision."""
    if target == pool.recovered:
        return target / f"{task_id}--{claim_id}.md"
    destination = target / f"{task_id}.md"
    if destination.exists():
        return None
    return destination


def recover(pool: Pool, task_ids: set[str], claim_ids: set[str], all_claims: bool, dry_run: bool, to: Path) -> int:
    """Move orphaned or parent-confirmed claims out of ``claimed/``.

    The script never guesses that a claimed task is dead: claims carrying a
    receipt are treated as possibly running and are only requeued when the
    parent explicitly selects them (``--task-id``/``--claim-id``/``--all``).
    Orphan receipts (receipt without its claimed file) are deterministic
    garbage and are removed; claimed files whose metadata is invalid cannot be
    a running worker's claim and are moved to ``rejected/``.
    """
    requeued: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    cleaned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    if pool.claimed.is_dir():
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
            validation_error = validate_task(claimed_file, task_id)
            if validation_error is not None:
                entry = {"task_id": task_id, "claim_id": claim_id, "path": str(claimed_file), "reason": validation_error}
                if dry_run:
                    rejected.append({**entry, "action": "would_reject"})
                    continue
                rejected_path = pool.rejected / f"{task_id}--{claim_id}.md"
                try:
                    pool.rejected.mkdir(parents=True, exist_ok=True)
                    claimed_file.replace(rejected_path)
                    remove_receipt(receipt)
                    rejected.append({**entry, "path": str(rejected_path)})
                except OSError as error:
                    failed.append({**entry, "reason": f"cannot move to rejected: {error}"})
                continue

            selected = all_claims or task_id in task_ids or claim_id in claim_ids
            if not selected:
                reason = "missing_receipt" if not receipt.is_file() else "running_or_unacknowledged"
                skipped.append({"task_id": task_id, "claim_id": claim_id, "path": str(claimed_file), "reason": reason})
                continue

            destination = requeue_destination(to, pool, task_id, claim_id)
            if destination is None:
                failed.append({"task_id": task_id, "claim_id": claim_id, "path": str(claimed_file), "reason": "requeue destination already exists"})
                continue
            if dry_run:
                requeued.append({"task_id": task_id, "claim_id": claim_id, "path": str(destination), "action": "would_requeue"})
                continue
            try:
                to.mkdir(parents=True, exist_ok=True)
                claimed_file.replace(destination)
                remove_receipt(receipt)
                requeued.append({"task_id": task_id, "claim_id": claim_id, "path": str(destination)})
            except OSError as error:
                failed.append({"task_id": task_id, "claim_id": claim_id, "path": str(claimed_file), "reason": f"cannot requeue: {error}"})

    if pool.claimed.is_dir():
        try:
            receipts = sorted(pool.claimed.glob("*.receipt"), key=lambda item: item.name)
        except OSError as error:
            emit({"status": "error", "reason": f"cannot scan receipts: {error}"})
            return 1
        for receipt in receipts:
            claimed_file = pool.claimed / receipt.name[: -len(".receipt")]
            if claimed_file.exists():
                continue
            entry = {"path": str(receipt), "reason": "orphan_receipt"}
            if dry_run:
                cleaned.append({**entry, "action": "would_clean"})
                continue
            try:
                receipt.unlink()
                cleaned.append(entry)
            except OSError as error:
                failed.append({**entry, "reason": f"cannot remove: {error}"})

    emit({
        "status": "recovered",
        "dry_run": dry_run,
        "requeued": requeued,
        "rejected": rejected,
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Atomically claim one pending DeepSeek delegation, or run recovery/locate helpers.")
    parser.add_argument("--workspace", default=".", help="pool root; defaults to the real cwd ('.deepseek-delegations' inside it)")
    subparsers = parser.add_subparsers(dest="command")

    recover_parser = subparsers.add_parser("recover", help="recover orphaned or parent-confirmed claimed tasks")
    recover_parser.add_argument("--task-id", action="append", default=[], help="requeue this task (repeatable; parent confirms its worker is dead)")
    recover_parser.add_argument("--claim-id", action="append", default=[], help="requeue this exact claim (repeatable)")
    recover_parser.add_argument("--all", action="store_true", help="requeue every claimed task; only after confirming no workers are running")
    recover_parser.add_argument("--dry-run", action="store_true", help="classify and report without moving anything")
    recover_parser.add_argument("--to", choices=("recovered", "pending"), default="recovered", help="requeue destination directory")

    locate_parser = subparsers.add_parser("locate", help="deterministically locate a claimed task file")
    locate_parser.add_argument("--task-id", required=True)
    locate_parser.add_argument("--claim-id", default=None)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        workspace = resolve_workspace(args.workspace)
    except OSError as error:
        emit({"status": "error", "reason": f"cannot resolve workspace: {error}"})
        return 1
    pool = Pool(workspace)

    real_cwd = Path.cwd().resolve()
    if workspace != real_cwd:
        emit({
            "status": "warning",
            "reason": "workspace differs from the real cwd; the task pool must live at the calling thread's real cwd",
            "workspace": str(workspace),
            "real_cwd": str(real_cwd),
        }, to_stderr=True)

    if args.command == "recover":
        target = pool.recovered if args.to == "recovered" else pool.pending
        return recover(pool, set(args.task_id), set(args.claim_id), args.all, args.dry_run, target)
    if args.command == "locate":
        return locate(pool, args.task_id, args.claim_id)
    return claim_one(pool)


if __name__ == "__main__":
    raise SystemExit(main())
