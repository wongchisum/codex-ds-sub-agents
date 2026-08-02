#!/usr/bin/env python3
"""Platform-aware runtime helpers for worker agents and adapter services.

Centralizes the places that previously assumed macOS/POSIX:

- ``python_command()`` / ``python_command_toml()`` render the current Python
  interpreter for generated worker instructions with TOML-safe quoting.
- ``codex_executable()`` discovers the Codex binary per platform.
- ``adapter_paths()`` resolves per-user adapter service directories.
- ``service_command()`` builds the adapter service command line.

Nothing here imports fcntl, plistlib, launchctl, or other platform-specific
modules unconditionally, so importing this module succeeds on Windows.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


def is_windows() -> bool:
    return sys.platform.startswith("win")


def python_command() -> str:
    """Return the absolute Python interpreter running this toolchain."""
    return os.path.abspath(sys.executable)


def toml_path_escape(value: str) -> str:
    """Escape a raw filesystem path for use inside a TOML basic string.

    Backslashes are doubled so Windows paths do not form invalid TOML escapes;
    POSIX paths are unchanged.
    """
    return value.replace("\\", "\\\\")


def python_command_toml() -> str:
    """Render the Python interpreter as a quoted, TOML-safe command token."""
    return json.dumps(python_command(), ensure_ascii=False)


def codex_executable() -> str | None:
    """Locate the Codex CLI binary on the current platform.

    Uses PATH first; on macOS falls back to the bundled Desktop binary. On
    Windows only PATH lookup is attempted (installer-specific paths are a
    real-machine validation item).
    """
    found = shutil.which("codex")
    if found:
        return found
    if sys.platform == "darwin":
        bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
        if bundled.is_file():
            return str(bundled)
    return None


@dataclass(frozen=True)
class AdapterPaths:
    scripts_dir: Path
    logs_dir: Path
    definitions_dir: Path


def adapter_paths(codex_home: Path) -> AdapterPaths:
    """Resolve per-user adapter service paths for the current platform.

    POSIX keeps definitions in the user LaunchAgents directory; Windows keeps
    generated Task Scheduler XML files under %LOCALAPPDATA% so no administrator
    rights are needed.
    """
    scripts_dir = codex_home / "adapters"
    logs_dir = codex_home / "logs" / "adapters"
    if is_windows():
        local = os.environ.get("LOCALAPPDATA") or str(Path.home())
        definitions_dir = Path(local) / "Codex" / "SubagentAdapters"
    else:
        definitions_dir = Path.home() / "Library" / "LaunchAgents"
    return AdapterPaths(scripts_dir=scripts_dir, logs_dir=logs_dir, definitions_dir=definitions_dir)


def service_command(
    python: str,
    adapter_script: Path,
    *,
    listen_host: str,
    port: int,
    service_id: str,
    max_output_tokens: int,
    upstream_base_url: str,
    model_catalog: Path,
    audit_log: Path,
) -> tuple[str, ...]:
    """Build the adapter service command line shared by all service backends."""
    return (
        python,
        str(adapter_script),
        "--listen", listen_host,
        "--port", str(port),
        "--service-id", service_id,
        "--max-output-tokens", str(max_output_tokens),
        "--upstream-base-url", upstream_base_url,
        "--model-catalog", str(model_catalog),
        "--audit-log", str(audit_log),
    )


def quote_windows_argument(value: str) -> str:
    """Quote one argument for a Windows command line (cmd-style).

    Values containing spaces or quotes are wrapped in double quotes; embedded
    quotes are backslash-escaped. Paths without whitespace pass through. This
    is intentionally a small, testable subset of ``CommandLineToArgvW`` rules.
    """
    if value == "":
        return '""'
    if not any(character in value for character in ' \t"'):
        return value
    return '"' + value.replace('"', '\\"') + '"'


def quote_windows_command(arguments: Sequence[str]) -> str:
    """Join arguments into one Windows command line."""
    return " ".join(quote_windows_argument(argument) for argument in arguments)
