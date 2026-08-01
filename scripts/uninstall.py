#!/usr/bin/env python3
"""Uninstall the DeepSeek provider, worker, model catalog, and delegation skill.

Only removes files that exactly match what scripts/install.py wrote. Files the
user created or modified are preserved and reported, never deleted. config.toml
is backed up before the installed provider block is removed.
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timezone
from pathlib import Path

from install import SKILL_MANIFEST, resolve_codex_home, skill_manifest_bytes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROVIDER_HEADER = "[model_providers.deepseek]"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Uninstall the DeepSeek subagent installation.")
    parser.add_argument("--codex-home", type=Path, default=resolve_codex_home())
    parser.add_argument("--dry-run", action="store_true", help="print planned actions without changing anything")
    return parser.parse_args()


def backup(destination: Path) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = destination.with_name(f"{destination.name}.bak.{timestamp}")
    shutil.copy2(destination, backup_path)
    print(f"backup: {backup_path}")


def rendered_agent(codex_home: Path) -> bytes:
    template = (PROJECT_ROOT / "agents" / "deepseek-worker.toml.template").read_text(encoding="utf-8")
    return template.replace("__CODEX_HOME__", str(codex_home)).encode("utf-8")


def remove_file_if_unchanged(path: Path, expected: bytes, dry_run: bool) -> str | None:
    if path.is_symlink():
        print(f"preserved (symlink): {path}")
        return "preserved"
    if not path.is_file():
        return None
    if path.read_bytes() != expected:
        print(f"preserved (modified): {path}")
        return "preserved"
    if dry_run:
        print(f"would remove: {path}")
        return "would-remove"
    path.unlink()
    print(f"removed: {path}")
    return "removed"


def uninstall_skill(codex_home: Path, dry_run: bool) -> None:
    source = PROJECT_ROOT / "skills" / "deepseek-delegation"
    destination = codex_home / "skills" / "deepseek-delegation"
    if not destination.exists():
        print(f"missing: {destination}")
        return
    if destination.is_symlink():
        print(f"preserved (symlink): {destination}")
        return
    for path in sorted(source.rglob("*")):
        if path.is_file():
            remove_file_if_unchanged(destination / path.relative_to(source), path.read_bytes(), dry_run)
    remove_file_if_unchanged(destination / SKILL_MANIFEST, skill_manifest_bytes(source), dry_run)
    if not dry_run and destination.exists():
        for directory in [*sorted(destination.rglob("*"), reverse=True), destination]:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        if destination.exists():
            print(f"preserved: {destination} (contains user files)")


def uninstall_provider(codex_home: Path, dry_run: bool) -> str:
    config_path = codex_home / "config.toml"
    if config_path.is_symlink():
        return "preserved (symlink)"
    if not config_path.is_file():
        return "missing"
    content = config_path.read_text(encoding="utf-8")
    if PROVIDER_HEADER not in content:
        return "preserved (no provider block)"
    provider = (PROJECT_ROOT / "config" / "deepseek-provider.toml").read_text(encoding="utf-8").strip()
    if content.endswith(f"\n\n{provider}\n"):
        updated = content[: -len(f"\n\n{provider}\n")]
    elif content == f"{provider}\n":
        updated = ""
    else:
        print(f"preserved (modified provider block): {config_path}")
        return "preserved"
    if dry_run:
        return "would-remove"
    backup(config_path)
    config_path.write_text(updated, encoding="utf-8")
    return "removed"


def main() -> int:
    args = parse_args()
    codex_home = args.codex_home.expanduser().resolve()
    print(f"codex home: {codex_home}")

    remove_file_if_unchanged(
        codex_home / "agents" / "deepseek-worker.toml",
        rendered_agent(codex_home),
        args.dry_run,
    )
    remove_file_if_unchanged(
        codex_home / "models" / "deepseek-v4-flash.json",
        (PROJECT_ROOT / "models" / "deepseek-v4-flash.json").read_bytes(),
        args.dry_run,
    )
    uninstall_skill(codex_home, args.dry_run)
    status = uninstall_provider(codex_home, args.dry_run)
    print(f"{status}: {codex_home / 'config.toml'}")

    print("next: reinstall with python3 scripts/install.py; config backups keep rollback copies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
