#!/usr/bin/env python3
"""Install the DeepSeek provider, worker, model catalog, and delegation skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROVIDER_HEADER = "[model_providers.deepseek]"
SKILL_MANIFEST = ".codex-deepseek-manifest.json"


class InstallError(Exception):
    """Refuse to continue when the install target violates the symlink policy."""


def resolve_codex_home() -> Path:
    """Return the effective Codex home, falling back when CODEX_HOME is blank."""
    raw = os.environ.get("CODEX_HOME")
    if raw is None or not raw.strip():
        return Path.home() / ".codex"
    return Path(raw)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", type=Path, default=resolve_codex_home())
    return parser.parse_args()


def toml_header_present(text: str, header: str) -> bool:
    """Return whether `header` appears as a real TOML table line, ignoring comments."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "#" in stripped:
            stripped = stripped.split("#", 1)[0].strip()
        if stripped == header:
            return True
    return False


def ensure_not_symlink(path: Path) -> None:
    if path.is_symlink():
        raise InstallError(
            f"refusing to write through symlink {path}; remove the symlink or replace it "
            "with a regular file/directory first"
        )


def backup_if_changed(destination: Path, content: bytes) -> None:
    if not destination.exists() or destination.read_bytes() == content:
        return
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = destination.with_name(f"{destination.name}.bak.{timestamp}")
    shutil.copy2(destination, backup)
    print(f"backup: {backup}")


def install_file(source: Path, destination: Path, replacements: dict[str, str] | None = None) -> None:
    ensure_not_symlink(destination)
    text = source.read_text(encoding="utf-8")
    for old, new in (replacements or {}).items():
        text = text.replace(old, new)
    content = text.encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup_if_changed(destination, content)
    destination.write_bytes(content)
    print(f"installed: {destination}")


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def skill_manifest(source: Path) -> dict[str, object]:
    files = {
        path.relative_to(source).as_posix(): file_digest(path)
        for path in sorted(source.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }
    return {"version": 1, "files": files}


def skill_manifest_bytes(source: Path) -> bytes:
    return (json.dumps(skill_manifest(source), indent=2, sort_keys=True) + "\n").encode("utf-8")


def read_skill_manifest(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    files = data.get("files") if isinstance(data, dict) and data.get("version") == 1 else None
    if not isinstance(files, dict):
        return {}
    return {
        relative: digest
        for relative, digest in files.items()
        if isinstance(relative, str) and isinstance(digest, str)
    }


def install_skill(codex_home: Path) -> None:
    source = PROJECT_ROOT / "skills" / "deepseek-delegation"
    destination = codex_home / "skills" / "deepseek-delegation"
    ensure_not_symlink(destination)
    manifest_path = destination / SKILL_MANIFEST
    previous_files = read_skill_manifest(manifest_path)
    if destination.exists():
        if not destination.is_dir():
            raise InstallError(f"refusing to install into non-directory {destination}")
        for path in source.rglob("*"):
            if path.is_file() and not path.is_symlink():
                relative = path.relative_to(source)
                install_file(path, destination / relative)
    else:
        shutil.copytree(source, destination)
        print(f"installed: {destination}")

    current_files = skill_manifest(source)["files"]
    assert isinstance(current_files, dict)
    for relative, expected_digest in previous_files.items():
        if relative in current_files:
            continue
        stale = destination / relative
        if stale.is_file() and not stale.is_symlink() and file_digest(stale) == expected_digest:
            stale.unlink()
            print(f"removed stale installed file: {stale}")
    for directory in sorted(destination.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        if directory.is_dir() and not directory.is_symlink():
            try:
                directory.rmdir()
            except OSError:
                pass
    ensure_not_symlink(manifest_path)
    manifest_path.write_bytes(skill_manifest_bytes(source))
    print(f"installed: {manifest_path}")


def install_provider(codex_home: Path) -> None:
    config_path = codex_home / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    ensure_not_symlink(config_path)
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if toml_header_present(existing, PROVIDER_HEADER):
        print("preserved: existing DeepSeek provider configuration")
        return

    provider = (PROJECT_ROOT / "config" / "deepseek-provider.toml").read_text(encoding="utf-8").strip()
    separator = "\n\n" if existing.strip() else ""
    content = f"{existing.rstrip()}{separator}{provider}\n".encode("utf-8")
    backup_if_changed(config_path, content)
    config_path.write_bytes(content)
    print(f"updated: {config_path}")


def main() -> int:
    args = parse_args()
    codex_home = args.codex_home.expanduser().resolve()
    try:
        codex_home.mkdir(parents=True, exist_ok=True)
        install_skill(codex_home)
        install_file(
            PROJECT_ROOT / "agents" / "deepseek-worker.toml.template",
            codex_home / "agents" / "deepseek-worker.toml",
            {"__CODEX_HOME__": str(codex_home)},
        )
        install_file(
            PROJECT_ROOT / "models" / "deepseek-v4-flash.json",
            codex_home / "models" / "deepseek-v4-flash.json",
        )
        install_provider(codex_home)
    except InstallError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print("next: store the API key in macOS Keychain, run scripts/doctor.py, then start a new Codex task")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
