#!/usr/bin/env python3
"""Collect reproducible, credential-free diagnostics for a Codex subagent run.

The tool builds a deterministic directory or zip bundle for one named run:

- platform / Python / Codex version metadata
- the sanitized manifest structure
- the installed selection and installation registry summary
- adapter health results and bounded adapter audit/log tails
- mailbox receipt and run-state summaries (never task markdown)

Every artifact records its source, collection status, timestamp, and any
error in ``diagnostics.json``; ``README.txt`` explains safe sharing.
Credential values, environment values, Keychain contents, task bodies,
prompts, claimed/pending markdown, config.toml contents, and encrypted
payloads are never collected. Included JSON/text is recursively redacted
for secret-like keys and bearer/API-key patterns, and file sizes and tail
lengths are bounded.
"""

from __future__ import annotations

import argparse
import json
import platform as platform_module
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from adapter_service import check_health_url, service_fingerprint
from install import INSTALL_REGISTRY, resolve_codex_home
from model_manifest import load_manifest

SCHEMA_VERSION = 1
TOOL_NAME = "codex-subagent-diagnostics"
RUN_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
REDACTED = "<redacted>"

DEFAULT_MAX_TAIL_LINES = 200
DEFAULT_MAX_TAIL_BYTES = 64 * 1024
DEFAULT_MAX_FILE_BYTES = 512 * 1024
DEFAULT_CODEX_TIMEOUT = 5.0
DEFAULT_HEALTH_TIMEOUT = 2.0
SUMMARY_MAX_CHARS = 200
MAILBOX_NAME = ".codex-custom-subagents"
LEGACY_MAILBOX_NAME = ".deepseek-delegations"

ADAPTER_LOG_SUFFIXES = (".audit.jsonl", ".stdout.log", ".stderr.log")
CONFIGURE_LOG_LIMIT = 5
BUNDLED_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex")

_SECRET_KEY_PATTERNS = (
    # suffix style: my_secret, api_key, client_secret, auth_token, refresh_token
    re.compile(
        r"(?i)(?:^|.*[_-])("
        r"api[_-]?key|access[_-]?key|private[_-]?key|refresh[_-]?token|"
        r"access[_-]?token|bearer[_-]?token|client[_-]?secret|session[_-]?key|"
        r"auth[_-]?(?:value|token|key|secret)|secret|token|password|passwd|"
        r"authorization|bearer|credential|credentials"
        r")$"
    ),
    # exact single-word keys
    re.compile(r"(?i)^(secret|token|password|passwd|authorization|bearer|credential|credentials|apikey)$"),
)

_TEXT_REDACTIONS = (
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?key|client[_-]?secret|authorization|password|passwd)\b\s*[:=]\s*[^\s,;\"']+"), lambda m: m.group(1) + "=" + REDACTED),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}"), lambda m: "bearer " + REDACTED),
    (re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b"), lambda m: REDACTED),
    (re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"), lambda m: m.group(0).split("://", 1)[0] + "://" + REDACTED + "@"),
    (re.compile(r"\b[0-9a-fA-F]{40,}\b"), lambda m: REDACTED),
)


class DiagnosticsError(ValueError):
    """Invalid run name, existing bundle, or unusable output directory."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _is_secret_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.lower().strip()
    for punctuation in ("-", ".", " "):
        normalized = normalized.replace(punctuation, "_")
    return any(pattern.fullmatch(normalized) is not None for pattern in _SECRET_KEY_PATTERNS)


def redact_text(text: str) -> str:
    for pattern, replacement in _TEXT_REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def redact_json(value: Any) -> Any:
    """Recursively redact secret-like keys and credential patterns in JSON values."""
    if isinstance(value, dict):
        return {
            key: (REDACTED if _is_secret_key(key) else redact_json(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def bounded_tail(path: Path, max_lines: int, max_bytes: int) -> tuple[bytes, dict[str, Any]]:
    """Return the last ``max_lines`` lines of ``path`` capped at ``max_bytes``."""
    total_bytes = path.stat().st_size
    read_size = min(total_bytes, max_bytes)
    with path.open("rb") as handle:
        if read_size == 0:
            content = b""
        else:
            start = max(0, total_bytes - read_size - 1)
            handle.seek(start)
            window = handle.read(total_bytes - start)
            if len(window) > read_size:
                # The window opens mid-line: drop the partial leading line.
                first_newline = window.find(b"\n")
                window = window[first_newline + 1 :] if first_newline != -1 else b""
            lines = window.split(b"\n")
            trailing_newline = bool(lines and lines[-1] == b"")
            if trailing_newline:
                lines = lines[:-1]
            content = b"\n".join(lines[-max_lines:])
            if trailing_newline and content:
                content += b"\n"
    meta = {
        "total_bytes": total_bytes,
        "tail_lines": content.count(b"\n"),
        "tail_bytes": len(content),
    }
    return content, meta


def bounded_read(path: Path, max_bytes: int) -> tuple[bytes, dict[str, Any]]:
    total_bytes = path.stat().st_size
    with path.open("rb") as handle:
        content = handle.read(max_bytes + 1)
    truncated = len(content) > max_bytes
    content = content[:max_bytes]
    return content, {"total_bytes": total_bytes, "truncated": truncated}


def probe_codex_version(timeout: float = DEFAULT_CODEX_TIMEOUT) -> dict[str, Any]:
    """Probe ``codex --version`` with a hard timeout; never uses a shell."""
    candidates: list[Path] = []
    on_path = shutil.which("codex")
    if on_path:
        candidates.append(Path(on_path))
    elif BUNDLED_CODEX.is_file():
        candidates.append(BUNDLED_CODEX)
    if not candidates:
        return {
            "status": "missing",
            "source": "codex",
            "detail": "codex binary not found on PATH or at the bundled path",
            "timed_out": False,
        }
    binary = candidates[0]
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "source": str(binary),
            "detail": f"timed out after {timeout:.0f}s",
            "timed_out": True,
        }
    except OSError as error:
        return {
            "status": "error",
            "source": str(binary),
            "detail": str(error),
            "timed_out": False,
        }
    detail = (result.stdout or result.stderr).strip()
    return {
        "status": "collected" if result.returncode == 0 else "error",
        "source": str(binary),
        "detail": detail or f"exit code {result.returncode}",
        "timed_out": False,
    }


@dataclass
class Collector:
    out: Path
    run: str
    fmt: str
    items: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.items = []
        self._zip_members: dict[str, bytes] = {} if self.fmt == "zip" else {}

    @property
    def bundle_path(self) -> Path:
        return self.out / f"{self.run}.zip" if self.fmt == "zip" else self.out / self.run

    def exists(self) -> bool:
        return self.bundle_path.exists()

    def write(self, relative: str, content: bytes, source: str, status: str, error: Optional[str], meta: Optional[dict[str, Any]] = None) -> None:
        item: dict[str, Any] = {
            "name": relative,
            "source": source,
            "status": status,
            "timestamp": utc_now(),
            "error": error,
        }
        if status == "collected":
            item["bytes"] = len(content)
            if meta is not None:
                for key, value in meta.items():
                    item[key] = value
        self.items.append(item)
        if status != "collected":
            return
        if self.fmt == "dir":
            target = self.bundle_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        else:
            self._zip_members[relative] = content

    def write_text(self, relative: str, text: str, source: str, status: str, error: Optional[str], meta: Optional[dict[str, Any]] = None) -> None:
        self.write(relative, text.encode("utf-8"), source, status, error, meta)

    def finish(self, created_at: str, readme: str) -> Path:
        self.write("README.txt", readme.encode("utf-8"), "generated", "collected", None)
        index = {
            "schema_version": SCHEMA_VERSION,
            "tool": TOOL_NAME,
            "run": self.run,
            "created_at": created_at,
            "items": sorted(self.items, key=lambda item: item["name"]),
            "excluded": [
                "credential values",
                "environment values",
                "Keychain contents",
                "task bodies and prompts",
                "claimed/pending/recovered task markdown",
                "config.toml contents",
                "encrypted payloads",
            ],
        }
        index_bytes = (json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        self.items.append({
            "name": "diagnostics.json",
            "source": "generated",
            "status": "collected",
            "timestamp": utc_now(),
            "error": None,
            "bytes": len(index_bytes),
        })
        if self.fmt == "dir":
            target = self.bundle_path / "diagnostics.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(index_bytes)
        else:
            self._zip_members["diagnostics.json"] = index_bytes
            self.bundle_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(self.bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, content in sorted(self._zip_members.items()):
                    archive.writestr(name, content)
        return self.bundle_path


def _json_load_bounded(path: Path, max_bytes: int) -> tuple[Any, Optional[str]]:
    try:
        raw, meta = bounded_read(path, max_bytes)
    except OSError as error:
        return None, f"cannot read: {error}"
    if meta.get("truncated"):
        return None, f"file exceeds {max_bytes} bytes; skipped to bound the bundle"
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, f"invalid JSON: {error}"
    if not isinstance(value, dict):
        return None, "expected a JSON object"
    return value, None


def _resolve_manifest_path(codex_home: Path, explicit: Optional[Path], max_bytes: int) -> tuple[Optional[Path], Optional[str]]:
    if explicit is not None:
        resolved = explicit.expanduser().resolve()
        return resolved, None
    registry_path = codex_home / INSTALL_REGISTRY
    registry = {}
    if registry_path.is_file():
        try:
            value, error = _json_load_bounded(registry_path, max_bytes)
            if error is None and isinstance(value, dict):
                registry = value
        except OSError:
            pass
    if isinstance(registry, dict):
        for section in ("installations", "pending"):
            records = registry.get(section)
            if not isinstance(records, dict):
                continue
            for record in sorted(records.values(), key=lambda value: str(value.get("manifest_path", ""))):
                if not isinstance(record, dict):
                    continue
                manifest_path = record.get("manifest_path")
                if isinstance(manifest_path, str) and Path(manifest_path).is_file():
                    return Path(manifest_path), None
    managed = codex_home / "custom-subagents" / "manifests"
    if managed.is_dir():
        try:
            candidates = sorted(path for path in managed.iterdir() if path.name.endswith(".json"))
        except OSError:
            candidates = []
        if candidates:
            return candidates[0], None
    return None, "no manifest found: pass --manifest or configure one first"


def _manifest_json(path: Optional[Path], max_bytes: int) -> tuple[Any, Optional[str]]:
    if path is None:
        return None, "manifest path not resolved"
    return _json_load_bounded(path, max_bytes)


def _manifest_structure(value: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """Return (provider ids with adapters, adapter blocks) from a raw manifest dict."""
    providers = value.get("providers")
    if not isinstance(providers, list):
        return [], []
    result = []
    used_ports: set[int] = set()
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        adapter = provider.get("local_adapter", provider.get("adapter"))
        if not isinstance(adapter, dict) and provider.get("upstream_protocol") == "anthropic_messages":
            adapter = {}
        if not isinstance(adapter, dict):
            continue
        listen_host = adapter.get("listen_host", "127.0.0.1")
        listen_port = adapter.get("listen_port")
        if listen_port is None:
            listen_port = 18766
            while listen_port in used_ports:
                listen_port += 1
        if not isinstance(listen_host, str) or not isinstance(listen_port, int):
            continue
        used_ports.add(listen_port)
        provider_id = provider.get("id")
        base_url = provider.get("base_url")
        if not isinstance(provider_id, str) or not isinstance(base_url, str):
            continue
        result.append({
            "provider_id": provider_id,
            "listen_host": listen_host,
            "listen_port": listen_port,
            "upstream_base_url": base_url,
        })
    return [entry["provider_id"] for entry in result], result


def _collect_platform() -> str:
    payload = {
        "platform": platform_module.platform(),
        "system": platform_module.system(),
        "machine": platform_module.machine(),
        "release": platform_module.release(),
        "python_implementation": platform_module.python_implementation(),
        "python_version": platform_module.python_version(),
        "python_executable": sys.executable,
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _collect_selection(codex_home: Path, collector: Collector, max_file_bytes: int) -> None:
    selection_path = codex_home / "models" / "subagent-selection.json"
    if not selection_path.is_file():
        collector.write_text(
            "install/selection.json", "{}", str(selection_path), "missing",
            "subagent-selection.json does not exist",
        )
        return
    value, error = _json_load_bounded(selection_path, max_file_bytes)
    if error is not None or value is None:
        collector.write_text(
            "install/selection.json", "{}", str(selection_path), "error", error,
        )
        return
    collector.write_text(
        "install/selection.json",
        json.dumps(redact_json(value), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        str(selection_path),
        "collected",
        None,
    )


def _collect_registry_summary(codex_home: Path, collector: Collector, max_file_bytes: int) -> None:
    registry_path = codex_home / INSTALL_REGISTRY
    if not registry_path.is_file():
        collector.write_text(
            "install/registry-summary.json", "{}", str(registry_path), "missing",
            "installation registry does not exist",
        )
        return
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        collector.write_text(
            "install/registry-summary.json", "{}", str(registry_path), "error",
            f"invalid installation registry: {error}",
        )
        return
    if not isinstance(registry, dict):
        collector.write_text(
            "install/registry-summary.json", "{}", str(registry_path), "error",
            "installation registry must be a JSON object",
        )
        return
    summary: dict[str, Any] = {"version": registry.get("version"), "order": registry.get("order", [])}
    for section in ("installations", "pending"):
        records = registry.get(section)
        entries = []
        if isinstance(records, dict):
            for installation_id in sorted(records):
                record = records[installation_id]
                if not isinstance(record, dict):
                    continue
                entries.append({
                    "id": installation_id,
                    "manifest_path": record.get("manifest_path"),
                    "providers": sorted(record.get("providers", {})) if isinstance(record.get("providers"), dict) else [],
                    "adapter_providers": sorted(record.get("adapter_providers", [])) if isinstance(record.get("adapter_providers"), list) else [],
                    "files": sorted(record.get("files", {})) if isinstance(record.get("files"), dict) else [],
                    "selection": redact_json(record.get("selection")) if isinstance(record.get("selection"), dict) else None,
                })
        summary[section] = entries
    collector.write_text(
        "install/registry-summary.json",
        json.dumps(redact_json(summary), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        str(registry_path),
        "collected",
        None,
    )


def _collect_adapter_health(
    adapters: list[dict[str, Any]],
    collector: Collector,
    skip: bool,
    health_timeout: float,
) -> None:
    source = "adapter health probes"
    if skip:
        collector.write_text(
            "adapters/health.json", json.dumps({"results": [], "skipped": True}, indent=2, sort_keys=True) + "\n",
            source, "collected", None,
        )
        return
    if not adapters:
        collector.write_text(
            "adapters/health.json", json.dumps({"results": [], "note": "no adapter providers configured"}, indent=2, sort_keys=True) + "\n",
            source, "collected", None,
        )
        return
    results = []
    for entry in adapters:
        provider_id = entry["provider_id"]
        health_url = f"http://{entry['listen_host']}:{entry['listen_port']}/health"
        fingerprint = service_fingerprint(provider_id, entry["upstream_base_url"])
        try:
            healthy, detail = check_health_url(health_url, provider_id, fingerprint, health_timeout)
        except Exception as error:  # probe failure must never abort collection
            healthy, detail = False, str(error)
        results.append({
            "provider_id": provider_id,
            "health_url": health_url,
            "healthy": healthy,
            "detail": redact_text(detail),
        })
    collector.write_text(
        "adapters/health.json",
        json.dumps({"results": results}, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        source,
        "collected",
        None,
    )


def _tail_adapter_logs(
    adapters: list[dict[str, Any]],
    codex_home: Path,
    collector: Collector,
    max_tail_lines: int,
    max_tail_bytes: int,
) -> None:
    log_dir = codex_home / "logs" / "adapters"
    provider_ids = sorted({entry["provider_id"] for entry in adapters})
    if log_dir.is_dir():
        try:
            candidates = sorted(path.name for path in log_dir.iterdir() if path.is_file())
        except OSError:
            candidates = []
        for name in candidates:
            for suffix in ADAPTER_LOG_SUFFIXES:
                if name.endswith(suffix):
                    provider_ids.append(name[: -len(suffix)])
        provider_ids = sorted(set(provider_ids))
    for provider_id in provider_ids:
        for suffix in ADAPTER_LOG_SUFFIXES:
            log_path = log_dir / f"{provider_id}{suffix}"
            relative = f"adapters/{provider_id}{suffix}.tail"
            if not log_path.is_file():
                collector.write_text(
                    relative, "", str(log_path), "missing", f"{log_path.name} does not exist",
                )
                continue
            try:
                content, meta = bounded_tail(log_path, max_tail_lines, max_tail_bytes)
            except OSError as error:
                collector.write_text(
                    relative, "", str(log_path), "error", f"cannot read tail: {error}",
                )
                continue
            safe = redact_text(content.decode("utf-8", errors="replace")).encode("utf-8")
            collector.write(relative, safe, str(log_path), "collected", None, meta)


def _tail_configure_logs(
    codex_home: Path,
    collector: Collector,
    max_tail_lines: int,
    max_tail_bytes: int,
) -> None:
    log_dir = codex_home / "logs" / "custom-subagents"
    try:
        candidates = sorted(
            (path for path in log_dir.glob("configure-*.jsonl") if path.is_file()),
            key=lambda path: path.name,
        )[-CONFIGURE_LOG_LIMIT:]
    except OSError:
        candidates = []
    if not candidates:
        collector.write_text(
            "configure/logs.jsonl.tail",
            "",
            str(log_dir),
            "missing",
            "no configure logs found",
        )
        return
    for index, log_path in enumerate(candidates, 1):
        try:
            content, meta = bounded_tail(log_path, max_tail_lines, max_tail_bytes)
        except OSError as error:
            collector.write_text(
                f"configure/{index:02d}.jsonl.tail",
                "",
                str(log_path),
                "error",
                f"cannot read tail: {error}",
            )
            continue
        safe = redact_text(content.decode("utf-8", errors="replace")).encode("utf-8")
        collector.write(
            f"configure/{index:02d}.jsonl.tail",
            safe,
            str(log_path),
            "collected",
            None,
            meta,
        )


def _collect_mailbox(workspace: Path, collector: Collector, max_file_bytes: int) -> None:
    current_mailbox = workspace / MAILBOX_NAME
    legacy_mailbox = workspace / LEGACY_MAILBOX_NAME
    mailbox = current_mailbox if current_mailbox.is_dir() else legacy_mailbox
    if not mailbox.is_dir():
        collector.write_text(
            "mailbox/summary.json",
            json.dumps(
                {
                    "mailbox": MAILBOX_NAME,
                    "receipts": [],
                    "run_states": [],
                    "note": "no custom-subagent mailbox present",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            str(current_mailbox),
            "missing",
            f"no {MAILBOX_NAME} mailbox",
        )
        return
    receipts = []
    for directory_name in ("claimed", "recovered", "rejected"):
        directory = mailbox / directory_name
        if not directory.is_dir():
            continue
        try:
            receipt_paths = sorted(path for path in directory.iterdir() if path.name.endswith(".receipt"))
        except OSError:
            continue
        for receipt_path in receipt_paths:
            value, error = _json_load_bounded(receipt_path, max_file_bytes)
            if error is not None or value is None:
                receipts.append({"receipt": receipt_path.name, "status": "error", "error": error})
                continue
            summary: dict[str, Any] = {}
            for key in (
                "schema_version", "status", "task_id", "claim_id", "attempt_id",
                "claimed_at", "completed_at", "exit_code",
                "parent_thread_id", "worker_thread_id", "agent", "model", "provider",
            ):
                if key in value:
                    summary[key] = value[key]
            if isinstance(value.get("summary"), str):
                summary["summary"] = value["summary"][:SUMMARY_MAX_CHARS]
            receipts.append(redact_json(summary))
    receipts.sort(key=lambda item: str(item.get("task_id", "")) + "|" + str(item.get("claim_id", "")))
    run_states = []
    runs_dir = mailbox / "runs"
    if runs_dir.is_dir():
        try:
            run_paths = sorted(path for path in runs_dir.iterdir() if path.name.endswith(".json"))
        except OSError:
            run_paths = []
        for run_path in run_paths:
            value, error = _json_load_bounded(run_path, max_file_bytes)
            if error is not None or value is None:
                run_states.append({"run_file": run_path.name, "status": "error", "error": error})
                continue
            run_states.append(redact_json(value))
    collector.write_text(
        "mailbox/summary.json",
        json.dumps(
            {
                "mailbox": mailbox.name,
                "legacy": mailbox.name == LEGACY_MAILBOX_NAME,
                "legacy_mailbox_present": legacy_mailbox.is_dir(),
                "receipts": receipts,
                "run_states": run_states,
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n",
        str(mailbox),
        "collected",
        None,
    )


def build_readme(run: str, bundle_path: Path, items: list[dict[str, Any]]) -> str:
    lines = [
        f"Codex custom-subagent diagnostics bundle",
        f"",
        f"Run: {run}",
        f"Bundle: {bundle_path}",
        f"Created: {utc_now()}",
        f"",
        f"Safe to share",
        f"  - This bundle is designed to be shared for cross-OS debugging without",
        f"    leaking credentials. It never contains credential values, environment",
        f"    values, Keychain contents, task bodies, prompts, claimed/pending",
        f"    markdown, config.toml contents, or encrypted payloads.",
        f"  - Included JSON/text is recursively redacted for secret-like keys and",
        f"    bearer/API-key patterns, and file sizes and tail lengths are bounded.",
        f"  - Before sharing, still skim README.txt and diagnostics.json for any",
        f"    host-specific path or name you do not want to expose.",
        f"",
        f"Contents",
    ]
    for item in sorted(items, key=lambda entry: entry["name"]):
        status = item.get("status", "unknown")
        marker = {"collected": "ok", "missing": "missing", "error": "error"}.get(status, status)
        lines.append(f"  [{marker}] {item.get('name', '?')}  (source: {item.get('source', '?')})")
        if item.get("error"):
            lines.append(f"        error: {item['error']}")
    lines.extend([
        f"",
        f"Limitations",
        f"  - Adapter health probes run only for adapters declared in the manifest",
        f"    and are skipped with --skip-adapter-health.",
        f"  - The Codex version probe has a timeout; a timeout is recorded, not",
        f"    retried.",
        f"  - Source paths are absolute and host-specific; filenames inside the",
        f"    bundle are deterministic for a given run name.",
        f"  - Missing files are reported in diagnostics.json and do not fail the",
        f"    collection.",
        f"",
        f"Regenerate",
        f"  {sys.executable} scripts/diagnostics.py --run {run} --out <output-dir> "
        f"[--format dir|zip]",
    ])
    return "\n".join(lines) + "\n"


def collect(
    *,
    run: str,
    out: Path,
    fmt: str = "dir",
    codex_home: Optional[Path] = None,
    workspace: Optional[Path] = None,
    manifest: Optional[Path] = None,
    max_tail_lines: int = DEFAULT_MAX_TAIL_LINES,
    max_tail_bytes: int = DEFAULT_MAX_TAIL_BYTES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    codex_timeout: float = DEFAULT_CODEX_TIMEOUT,
    health_timeout: float = DEFAULT_HEALTH_TIMEOUT,
    skip_adapter_health: bool = False,
    skip_mailbox: bool = False,
    probe_codex: bool = True,
) -> tuple[Path, list[dict[str, Any]]]:
    if RUN_ID_PATTERN.fullmatch(run) is None:
        raise DiagnosticsError(
            f"invalid run name {run!r}: must match {RUN_ID_PATTERN.pattern}"
        )
    if fmt not in ("dir", "zip"):
        raise DiagnosticsError(f"invalid format {fmt!r}: choose 'dir' or 'zip'")
    out = out.expanduser().resolve()
    if out.exists() and not out.is_dir():
        raise DiagnosticsError(f"output path exists and is not a directory: {out}")
    codex_home = (codex_home or resolve_codex_home()).expanduser().resolve()
    workspace = (workspace or Path.cwd()).expanduser().resolve()

    collector = Collector(out=out, run=run, fmt=fmt)
    if collector.exists():
        raise DiagnosticsError(f"bundle already exists (refusing to overwrite): {collector.bundle_path}")
    created_at = utc_now()

    collector.write_text(
        "meta/platform.json", _collect_platform(), "platform metadata", "collected", None,
    )
    if probe_codex:
        version = probe_codex_version(codex_timeout)
        collector.write_text(
            "meta/codex-version.json",
            json.dumps(version, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            "codex version probe",
            "collected",
            None,
        )
    else:
        collector.write_text(
            "meta/codex-version.json", "{}\n", "codex version probe", "skipped",
            "version probing disabled",
        )

    manifest_path, manifest_error = _resolve_manifest_path(codex_home, manifest, max_file_bytes)
    manifest_value, manifest_json_error = _manifest_json(manifest_path, max_file_bytes)
    if manifest_error is not None and manifest_path is None:
        collector.write_text(
            "manifest/manifest.json", "{}", "manifest", "missing", manifest_error,
        )
    elif manifest_json_error is not None or manifest_value is None:
        collector.write_text(
            "manifest/manifest.json", "{}", str(manifest_path), "error", manifest_json_error,
        )
    else:
        collector.write_text(
            "manifest/manifest.json",
            json.dumps(redact_json(manifest_value), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            str(manifest_path),
            "collected",
            None,
        )

    adapter_ids, adapters = _manifest_structure(manifest_value) if manifest_value is not None else ([], [])
    _collect_selection(codex_home, collector, max_file_bytes)
    _collect_registry_summary(codex_home, collector, max_file_bytes)
    _collect_adapter_health(adapters, collector, skip_adapter_health, health_timeout)
    _tail_adapter_logs(adapters, codex_home, collector, max_tail_lines, max_tail_bytes)
    _tail_configure_logs(codex_home, collector, max_tail_lines, max_tail_bytes)
    if not skip_mailbox:
        _collect_mailbox(workspace, collector, max_file_bytes)

    items_before_index = list(collector.items)
    bundle_path = collector.finish(created_at, build_readme(run, collector.bundle_path, items_before_index))
    return bundle_path, list(collector.items)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="diagnostics.py",
        description=__doc__,
    )
    parser.add_argument("--run", required=True, help="run name matching [a-z0-9][a-z0-9_-]{0,63}")
    parser.add_argument("--out", type=Path, default=Path("."), help="output directory (default: current directory)")
    parser.add_argument("--format", dest="fmt", choices=("dir", "zip"), default="dir", help="bundle format (default: dir)")
    parser.add_argument("--codex-home", type=Path, default=None, help="Codex home to inspect (default: resolved CODEX_HOME)")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="workspace containing .codex-custom-subagents (default: cwd)",
    )
    parser.add_argument("--manifest", type=Path, default=None, help="explicit manifest JSON to collect (default: from registry)")
    parser.add_argument("--max-tail-lines", type=int, default=DEFAULT_MAX_TAIL_LINES)
    parser.add_argument("--max-tail-bytes", type=int, default=DEFAULT_MAX_TAIL_BYTES)
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    parser.add_argument("--codex-timeout", type=float, default=DEFAULT_CODEX_TIMEOUT)
    parser.add_argument("--health-timeout", type=float, default=DEFAULT_HEALTH_TIMEOUT)
    parser.add_argument("--skip-adapter-health", action="store_true")
    parser.add_argument("--skip-mailbox", action="store_true")
    parser.add_argument("--no-codex-probe", action="store_true", help="skip the codex version probe")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        bundle_path, _items = collect(
            run=args.run,
            out=args.out,
            fmt=args.fmt,
            codex_home=args.codex_home,
            workspace=args.workspace,
            manifest=args.manifest,
            max_tail_lines=args.max_tail_lines,
            max_tail_bytes=args.max_tail_bytes,
            max_file_bytes=args.max_file_bytes,
            codex_timeout=args.codex_timeout,
            health_timeout=args.health_timeout,
            skip_adapter_health=args.skip_adapter_health,
            skip_mailbox=args.skip_mailbox,
            probe_codex=not args.no_codex_probe,
        )
    except DiagnosticsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"created: {bundle_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
