#!/usr/bin/env python3
"""Persist deterministic model fallback state for one custom-subagent run."""

from __future__ import annotations

import argparse
import enum
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from platform_lock import PlatformLockError, platform_file_lock


RUN_ID_PATTERN = re.compile(r"[a-z0-9_]{1,64}")
MAX_FAILURE_MESSAGE_CHARS = 8192
ELIGIBLE_FAILURES = frozenset(
    {"network", "timeout", "rate_limit", "billing", "service_unavailable"}
)
KNOWN_FAILURES = ELIGIBLE_FAILURES | frozenset(
    {"auth", "invalid_request", "model_not_found", "task_failure", "unknown"}
)
AUTH_PREFLIGHT_TIMEOUT_SECONDS = 5.0
MAILBOX_NAME = ".codex-custom-subagents"
LEGACY_MAILBOX_NAME = ".deepseek-delegations"


class RuntimeErrorCode(str, enum.Enum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    AUTH_UNAVAILABLE = "auth_unavailable"


class RuntimeFailure(RuntimeError):
    def __init__(self, code: RuntimeErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any], *, error: bool = False) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr if error else sys.stdout)


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeFailure(RuntimeErrorCode.INVALID_INPUT, f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeFailure(RuntimeErrorCode.INVALID_INPUT, f"{label} must be a JSON object")
    return value


def validate_selection(raw: dict[str, Any]) -> tuple[str, list[str], int, dict[str, dict[str, Any]]]:
    selection = raw.get("selection")
    models = raw.get("models")
    if not isinstance(selection, dict) or not isinstance(models, dict):
        raise RuntimeFailure(RuntimeErrorCode.INVALID_INPUT, "selection file requires selection and models objects")
    primary = selection.get("primary")
    fallbacks = selection.get("fallbacks", [])
    max_switches = selection.get("max_switches", len(fallbacks) if isinstance(fallbacks, list) else 0)
    if not isinstance(primary, str) or primary not in models:
        raise RuntimeFailure(RuntimeErrorCode.INVALID_INPUT, "selection.primary must reference a configured model")
    if not isinstance(fallbacks, list) or not all(isinstance(item, str) for item in fallbacks):
        raise RuntimeFailure(RuntimeErrorCode.INVALID_INPUT, "selection.fallbacks must be an array of model ids")
    if primary in fallbacks or len(set(fallbacks)) != len(fallbacks) or any(item not in models for item in fallbacks):
        raise RuntimeFailure(RuntimeErrorCode.INVALID_INPUT, "fallback ids must be unique configured models excluding primary")
    if not isinstance(max_switches, int) or isinstance(max_switches, bool) or max_switches < 0:
        raise RuntimeFailure(RuntimeErrorCode.INVALID_INPUT, "selection.max_switches must be a non-negative integer")
    normalized_models: dict[str, dict[str, Any]] = {}
    for model_id in [primary, *fallbacks]:
        model = models.get(model_id)
        if not isinstance(model, dict):
            raise RuntimeFailure(RuntimeErrorCode.INVALID_INPUT, f"model {model_id} must be an object")
        required = ("agent", "provider", "remote_model")
        if any(not isinstance(model.get(key), str) or not model[key] for key in required):
            raise RuntimeFailure(RuntimeErrorCode.INVALID_INPUT, f"model {model_id} lacks agent/provider/remote_model")
        normalized = {key: model[key] for key in required}
        if "auth" in model:
            normalized["auth"] = _normalize_auth_metadata(model["auth"], model_id)
        normalized_models[model_id] = normalized
    return primary, fallbacks, min(max_switches, len(fallbacks)), normalized_models


def classify_failure(
    *, category: str | None = None, http_status: int | None = None,
    error_code: str | None = None, message: str | None = None,
) -> str:
    if category is not None:
        if category not in KNOWN_FAILURES:
            raise RuntimeFailure(RuntimeErrorCode.INVALID_INPUT, f"unknown failure category: {category}")
        return category
    code = (error_code or "").lower()
    text = (message or "").lower()
    if http_status in (401, 403) or code in {"authentication_error", "auth"}:
        return "auth"
    billing_markers = (
        "insufficient credit", "credit balance", "credits exhausted", "payment required",
    )
    if http_status == 402 or "billing" in code or any(marker in text for marker in billing_markers):
        return "billing"
    if http_status == 429 or "rate_limit" in code or "rate limit" in text:
        return "rate_limit"
    if http_status in (408, 504) or "timeout" in code or "timed out" in text or "deadline exceeded" in text:
        return "timeout"
    if http_status in (500, 502, 503, 529) or code in {
        "overloaded_error", "upstream_invalid_response", "upstream_unavailable",
        "service_unavailable",
    }:
        return "service_unavailable"
    if http_status in (400, 422) or code in {"invalid_request", "invalid_request_error"}:
        return "invalid_request"
    if code == "model_not_found" or (http_status == 404 and "model" in text):
        return "model_not_found"
    network_markers = (
        "stream disconnected", "connection refused", "connection reset", "failed to connect",
        "dns", "name or service not known", "network is unreachable", "error sending request",
        "nodename nor servname provided", "getaddrinfo failed",
    )
    if any(marker in text for marker in network_markers) or code in {"network", "connection_error"}:
        return "network"
    return "unknown"


def pool_root(workspace: Path, *, legacy_mailbox: bool = False) -> Path:
    mailbox_name = LEGACY_MAILBOX_NAME if legacy_mailbox else MAILBOX_NAME
    return workspace.resolve() / mailbox_name / "runs"


def state_path(workspace: Path, run_id: str, *, legacy_mailbox: bool = False) -> Path:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise RuntimeFailure(RuntimeErrorCode.INVALID_INPUT, "run_id must match [a-z0-9_]{1,64}")
    return pool_root(workspace, legacy_mailbox=legacy_mailbox) / f"{run_id}.json"


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    try:
        with platform_file_lock(lock_path, exclusive=True):
            yield
    except PlatformLockError as error:
        raise OSError(str(error)) from error


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def active_descriptor(state: dict[str, Any]) -> dict[str, Any]:
    model_id = state["active_model_id"]
    model = state["models"][model_id]
    return {"model_id": model_id, **model}


def _codex_home_for_selection(selection_path: Path) -> Path:
    """Resolve the installed Codex home from its standard selection path."""
    resolved = selection_path.resolve()
    if resolved.parent.name.lower() == "models":
        return resolved.parent.parent
    return resolved.parent


def _normalize_auth_metadata(raw: object, model_id: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise RuntimeFailure(RuntimeErrorCode.INVALID_INPUT, f"model {model_id} auth must be an object")
    auth_type = raw.get("type")
    name = raw.get("name")
    if not isinstance(auth_type, str) or not auth_type:
        raise RuntimeFailure(RuntimeErrorCode.INVALID_INPUT, f"model {model_id} auth.type must be a non-empty string")
    if not isinstance(name, str) or not name:
        raise RuntimeFailure(RuntimeErrorCode.INVALID_INPUT, f"model {model_id} auth.name must be a non-empty string")
    if auth_type == "keychain":
        account = raw.get("account", "codex")
        if not isinstance(account, str) or not account:
            raise RuntimeFailure(RuntimeErrorCode.INVALID_INPUT, f"model {model_id} auth.account must be a non-empty string")
        return {"type": auth_type, "name": name, "account": account}
    if auth_type == "env":
        return {"type": auth_type, "name": name}
    if auth_type == "env_header":
        header = raw.get("header")
        if not isinstance(header, str) or not header:
            raise RuntimeFailure(RuntimeErrorCode.INVALID_INPUT, f"model {model_id} auth.header must be a non-empty string")
        return {"type": auth_type, "name": name, "header": header}
    raise RuntimeFailure(RuntimeErrorCode.INVALID_INPUT, f"model {model_id} auth.type is unsupported: {auth_type}")


def _auth_unavailable(model_id: str, detail: str) -> RuntimeFailure:
    return RuntimeFailure(
        RuntimeErrorCode.AUTH_UNAVAILABLE,
        f"authentication preflight failed for model {model_id}: {detail}; "
        "worker was not started and no bearer credential was sent",
    )


def _auth_preflight(selection_path: Path, model_id: str, model: dict[str, Any]) -> dict[str, str]:
    """Verify the credential reference in the same process context as delegation."""
    raw_auth = model.get("auth")
    if raw_auth is None:
        return {"status": "not_declared"}
    auth = _normalize_auth_metadata(raw_auth, model_id)
    auth_type = auth["type"]
    if auth_type in {"env", "env_header"}:
        if not os.environ.get(auth["name"]):
            raise _auth_unavailable(
                model_id,
                f"environment variable {auth['name']} is unavailable in the current Codex process",
            )
        return {**auth, "status": "passed"}

    helper = _codex_home_for_selection(selection_path) / "helpers" / "credential_store.py"
    if not helper.is_file():
        raise _auth_unavailable(model_id, f"credential helper is missing: {helper}")
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(helper),
                "exists",
                "--account",
                auth["account"],
                "--service",
                auth["name"],
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=AUTH_PREFLIGHT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise _auth_unavailable(model_id, "credential store lookup timed out") from None
    except OSError as error:
        raise _auth_unavailable(model_id, f"credential store lookup could not start: {error}") from error
    if result.returncode != 0:
        raise _auth_unavailable(
            model_id,
            f"credential {auth['name']} for account {auth['account']} is unavailable in the current process context",
        )
    return {**auth, "status": "passed"}


def begin(
    workspace: Path,
    run_id: str,
    selection_path: Path,
    *,
    legacy_mailbox: bool = False,
) -> dict[str, Any]:
    path = state_path(workspace, run_id, legacy_mailbox=legacy_mailbox)
    primary, fallbacks, max_switches, models = validate_selection(load_json(selection_path, "selection"))
    with run_lock(path):
        if path.exists():
            raise RuntimeFailure(RuntimeErrorCode.CONFLICT, f"run already exists: {run_id}")
        auth_preflight = {
            model_id: _auth_preflight(selection_path, model_id, model)
            for model_id, model in models.items()
        }
        state = {
            "schema_version": 1,
            "run_id": run_id,
            "status": "running",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "active_model_id": primary,
            "fallbacks": fallbacks,
            "max_switches": max_switches,
            "switch_count": 0,
            "attempted": [primary],
            "models": models,
            "auth_preflight": auth_preflight,
            "failures": [],
        }
        atomic_write(path, state)
    return {
        "status": "started",
        "run_id": run_id,
        "path": str(path),
        "active": active_descriptor(state),
        "auth_preflight": auth_preflight[primary],
    }


def record_failure(
    workspace: Path,
    run_id: str,
    failure: dict[str, Any],
    *,
    legacy_mailbox: bool = False,
) -> dict[str, Any]:
    path = state_path(workspace, run_id, legacy_mailbox=legacy_mailbox)
    message = failure.get("message")
    if message is not None and (
        not isinstance(message, str) or len(message) > MAX_FAILURE_MESSAGE_CHARS
    ):
        raise RuntimeFailure(
            RuntimeErrorCode.INVALID_INPUT,
            f"failure message must be a string of at most {MAX_FAILURE_MESSAGE_CHARS} characters",
        )
    with run_lock(path):
        if not path.is_file():
            raise RuntimeFailure(RuntimeErrorCode.NOT_FOUND, f"run not found: {run_id}")
        state = load_json(path, "run state")
        if state.get("status") != "running":
            raise RuntimeFailure(RuntimeErrorCode.CONFLICT, f"run is not active: {run_id}")
        category = classify_failure(**failure)
        state["failures"].append({
            "at": now_iso(), "model_id": state["active_model_id"], "category": category,
            "http_status": failure.get("http_status"), "error_code": failure.get("error_code"),
            "message_present": message is not None,
            "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest()
            if message is not None else None,
        })
        switched = False
        if category in ELIGIBLE_FAILURES and state["switch_count"] < state["max_switches"]:
            candidate = next((item for item in state["fallbacks"] if item not in state["attempted"]), None)
            if candidate is not None:
                state["active_model_id"] = candidate
                state["attempted"].append(candidate)
                state["switch_count"] += 1
                switched = True
        exhausted = category in ELIGIBLE_FAILURES and not switched
        if exhausted:
            state["status"] = "exhausted"
        state["updated_at"] = now_iso()
        atomic_write(path, state)
    result = {
        "status": "switched" if switched else state["status"], "run_id": run_id,
        "category": category, "switched": switched, "exhausted": exhausted,
        "attempted": state["attempted"], "switch_count": state["switch_count"], "path": str(path),
    }
    if state["status"] == "running":
        result["active"] = active_descriptor(state)
    return result


def show(workspace: Path, run_id: str, *, legacy_mailbox: bool = False) -> dict[str, Any]:
    path = state_path(workspace, run_id, legacy_mailbox=legacy_mailbox)
    if not path.is_file():
        raise RuntimeFailure(RuntimeErrorCode.NOT_FOUND, f"run not found: {run_id}")
    return load_json(path, "run state")


def finish(
    workspace: Path,
    run_id: str,
    outcome: str,
    *,
    legacy_mailbox: bool = False,
) -> dict[str, Any]:
    path = state_path(workspace, run_id, legacy_mailbox=legacy_mailbox)
    with run_lock(path):
        if not path.is_file():
            raise RuntimeFailure(RuntimeErrorCode.NOT_FOUND, f"run not found: {run_id}")
        state = load_json(path, "run state")
        if state.get("status") in {"completed", "blocked"}:
            if state["status"] != outcome:
                raise RuntimeFailure(RuntimeErrorCode.CONFLICT, f"run already finished as {state['status']}")
            return {"status": state["status"], "run_id": run_id, "path": str(path), "idempotent": True}
        state["status"] = outcome
        state["finished_at"] = now_iso()
        state["updated_at"] = state["finished_at"]
        atomic_write(path, state)
    return {"status": outcome, "run_id": run_id, "path": str(path), "idempotent": False}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=".")
    parser.add_argument(
        "--legacy-mailbox",
        action="store_true",
        help="use legacy .deepseek-delegations run state only for pre-upgrade work",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("begin")
    start.add_argument("--run-id", required=True)
    start.add_argument("--selection", type=Path, required=True)
    failure = commands.add_parser("record-failure")
    failure.add_argument("--run-id", required=True)
    failure.add_argument("--category", choices=sorted(KNOWN_FAILURES))
    failure.add_argument("--http-status", type=int)
    failure.add_argument("--error-code")
    failure.add_argument("--message")
    status = commands.add_parser("status")
    status.add_argument("--run-id", required=True)
    done = commands.add_parser("finish")
    done.add_argument("--run-id", required=True)
    done.add_argument("--outcome", choices=("completed", "blocked"), required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()
    try:
        if args.command == "begin":
            result = begin(
                workspace,
                args.run_id,
                args.selection.expanduser().resolve(),
                legacy_mailbox=args.legacy_mailbox,
            )
        elif args.command == "record-failure":
            result = record_failure(workspace, args.run_id, {
                "category": args.category, "http_status": args.http_status,
                "error_code": args.error_code, "message": args.message,
            }, legacy_mailbox=args.legacy_mailbox)
        elif args.command == "status":
            result = show(workspace, args.run_id, legacy_mailbox=args.legacy_mailbox)
        else:
            result = finish(
                workspace,
                args.run_id,
                args.outcome,
                legacy_mailbox=args.legacy_mailbox,
            )
    except RuntimeFailure as error:
        emit({"status": "error", "code": error.code.value, "message": str(error)}, error=True)
        return 2
    emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
