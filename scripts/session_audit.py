#!/usr/bin/env python3
"""Audit a native subagent rollout without trusting the worker's final report."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


class AuditError(RuntimeError):
    pass


UPSTREAM_INVALID_PATTERN = re.compile(
    r"(?<![a-z0-9_])upstream_invalid_response(?![a-z0-9_])",
    re.IGNORECASE,
)


def is_upstream_invalid_response(error: dict[str, Any]) -> bool:
    for key in ("type", "code", "error_code"):
        value = error.get(key)
        if isinstance(value, str) and value.lower() == "upstream_invalid_response":
            return True
    message = error.get("message")
    return isinstance(message, str) and UPSTREAM_INVALID_PATTERN.search(message) is not None


def records(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    raise AuditError(f"invalid JSON on rollout line {number}: {error}") from error
                if isinstance(value, dict):
                    yield value
    except (OSError, UnicodeError) as error:
        raise AuditError(f"cannot read rollout: {error}") from error


def load_selection(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AuditError(f"cannot read selection: {error}") from error
    return value if isinstance(value, dict) else {}


def output_tokens(payload: dict[str, Any]) -> int | None:
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    usage = info.get("last_token_usage") or info.get("total_token_usage")
    if not isinstance(usage, dict):
        return None
    value = usage.get("output_tokens")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def audit(rollout: Path, selection_path: Path | None = None) -> dict[str, Any]:
    selection = load_selection(selection_path)
    selected_models = selection.get("models") if isinstance(selection.get("models"), dict) else {}
    result: dict[str, Any] = {
        "rollout": str(rollout),
        "parent_thread_id": None,
        "worker_thread_id": None,
        "agent": None,
        "model": None,
        "provider": None,
        "declared_context_window": None,
        "actual_model_context_window": None,
        "turn_count": 0,
        "first_turn_direct_nonempty_result": False,
        "follow_up_occurred": False,
        "last_output_tokens": None,
        "upstream_invalid_response": False,
        "last_error": None,
    }
    completed_turns = []
    for record in records(rollout):
        record_type = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record_type == "session_meta":
            result["worker_thread_id"] = payload.get("id")
            result["provider"] = payload.get("model_provider")
            source = payload.get("source")
            if isinstance(source, dict):
                subagent = source.get("subagent")
                spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
                if isinstance(spawn, dict):
                    result["parent_thread_id"] = spawn.get("parent_thread_id")
                    result["agent"] = spawn.get("agent_role")
        elif record_type == "turn_context":
            result["turn_count"] += 1
            result["model"] = payload.get("model")
        elif record_type == "event_msg":
            if payload.get("type") == "task_started":
                value = payload.get("model_context_window")
                if isinstance(value, int) and not isinstance(value, bool):
                    result["actual_model_context_window"] = value
            elif payload.get("type") == "token_count":
                value = output_tokens(payload)
                if value is not None:
                    result["last_output_tokens"] = value
            elif payload.get("type") == "task_complete":
                completed_turns.append(payload)
                error = payload.get("error")
                if isinstance(error, dict):
                    result["last_error"] = error
                    if is_upstream_invalid_response(error):
                        result["upstream_invalid_response"] = True
    if completed_turns:
        first = completed_turns[0]
        message = first.get("last_agent_message")
        result["first_turn_direct_nonempty_result"] = isinstance(message, str) and bool(message.strip())
        last = completed_turns[-1]
        if isinstance(last.get("error"), dict):
            result["last_error"] = last["error"]
    result["follow_up_occurred"] = result["turn_count"] > 1
    model_id = result["model"]
    for candidate_id, candidate in selected_models.items():
        if not isinstance(candidate, dict):
            continue
        provider_matches = (
            result["provider"] is None or candidate.get("provider") == result["provider"]
        )
        if provider_matches and (
            candidate_id == model_id or candidate.get("remote_model") == model_id
        ):
            value = candidate.get("context_window")
            if isinstance(value, int) and not isinstance(value, bool):
                result["declared_context_window"] = value
            break
    actual = result["actual_model_context_window"]
    declared = result["declared_context_window"]
    result["context_matches_declaration"] = (
        actual == declared if actual is not None and declared is not None else None
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollout", type=Path, required=True)
    parser.add_argument("--selection", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = audit(args.rollout.expanduser().resolve(), args.selection.expanduser().resolve() if args.selection else None)
    except AuditError as error:
        print(json.dumps({"status": "error", "message": str(error)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
