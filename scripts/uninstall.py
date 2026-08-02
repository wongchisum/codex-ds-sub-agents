#!/usr/bin/env python3
"""Uninstall custom subagent providers, workers, model catalogs, and the delegation skill.

Only removes files that exactly match what scripts/install.py wrote. Files the
user created or modified are preserved and reported, never deleted. config.toml
is backed up before the installed provider block is removed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from install import (
    INSTALL_REGISTRY_VERSION,
    SKILL_MANIFEST,
    SKILL_NAME,
    InstallError,
    atomic_write,
    manifest_installation_id,
    read_install_registry,
    remove_managed_legacy_skills,
    resolve_codex_home,
    skill_manifest_bytes,
    write_install_registry,
)

from platform_runtime import python_command_toml, toml_path_escape
from model_manifest import load_manifest
from model_selection import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROVIDER_HEADER = "[model_providers.deepseek]"
CUSTOM_AGENT_MARKER = "This worker is model candidate"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Uninstall a Codex Custom Subagents installation.")
    parser.add_argument("--codex-home", type=Path, default=resolve_codex_home())
    parser.add_argument("--dry-run", action="store_true", help="print planned actions without changing anything")
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument(
        "--no-stop-adapters",
        action="store_true",
        help="remove installed files without stopping persistent adapter LaunchAgents",
    )
    return parser.parse_args()


def backup(destination: Path) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = destination.with_name(f"{destination.name}.bak.{timestamp}")
    shutil.copy2(destination, backup_path)
    print(f"backup: {backup_path}")


def rendered_agent(codex_home: Path) -> bytes:
    template = (PROJECT_ROOT / "agents" / "deepseek-worker.toml.template").read_text(encoding="utf-8")
    rendered = template.replace("__CODEX_HOME__", toml_path_escape(str(codex_home)))
    rendered = rendered.replace("__PYTHON_COMMAND__", python_command_toml())
    return rendered.encode("utf-8")


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


def remove_file_if_digest(path: Path, expected_digest: str, dry_run: bool) -> str | None:
    if path.is_symlink():
        print(f"preserved (symlink): {path}")
        return "preserved"
    if not path.is_file():
        return None
    actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_digest != expected_digest:
        print(f"preserved (modified): {path}")
        return "preserved"
    if dry_run:
        print(f"would remove: {path}")
        return "would-remove"
    path.unlink()
    print(f"removed: {path}")
    return "removed"


def recorded_selection_bytes(record: dict[str, object]) -> bytes:
    return (json.dumps(record["selection"], indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def uninstall_skill(codex_home: Path, dry_run: bool) -> None:
    source = PROJECT_ROOT / "skills" / SKILL_NAME
    destination = codex_home / "skills" / SKILL_NAME
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


def uninstall_skills(codex_home: Path, dry_run: bool) -> None:
    """Remove the current skill and all owned managed legacy skill installs."""
    uninstall_skill(codex_home, dry_run)
    remove_managed_legacy_skills(codex_home, dry_run)


def custom_agent_paths(codex_home: Path) -> set[Path]:
    agents = codex_home / "agents"
    if not agents.is_dir():
        return set()
    result = set()
    for path in agents.glob("*.toml"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if CUSTOM_AGENT_MARKER in content:
            result.add(path)
    return result


def _without_exact_block(content: str, block: str) -> str | None:
    """Remove one exact managed TOML block, including from the middle of config."""
    normalized = block.strip()
    candidates = (
        (normalized + "\n\n", ""),
        ("\n\n" + normalized + "\n\n", "\n\n"),
        ("\n\n" + normalized + "\n", ""),
        (normalized + "\n", ""),
        (normalized, ""),
    )
    for needle, replacement in candidates:
        if needle in content:
            return content.replace(needle, replacement, 1)
    return None


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
    updated = _without_exact_block(content, provider)
    if updated is None:
        print(f"preserved (modified provider block): {config_path}")
        return "preserved"
    if dry_run:
        return "would-remove"
    backup(config_path)
    config_path.write_text(updated, encoding="utf-8")
    return "removed"


def uninstall_provider_block(codex_home: Path, provider: str, dry_run: bool) -> str:
    config_path = codex_home / "config.toml"
    if config_path.is_symlink():
        return "preserved (symlink)"
    if not config_path.is_file():
        return "missing"
    content = config_path.read_text(encoding="utf-8")
    block = provider.strip()
    updated = _without_exact_block(content, block)
    if updated is None:
        return "preserved (modified or missing)"
    if dry_run:
        return "would-remove"
    backup(config_path)
    config_path.write_text(updated, encoding="utf-8")
    return "removed"


def stop_adapter_services(
    codex_home: Path,
    manifest_path: Path,
    dry_run: bool,
    provider_ids: Optional[Iterable[str]] = None,
) -> None:
    selected = None if provider_ids is None else tuple(sorted(set(provider_ids)))
    if selected == ():
        return
    if dry_run:
        suffix = "" if selected is None else " (providers: " + ", ".join(selected) + ")"
        print(f"would stop adapter services from: {manifest_path}{suffix}")
        return
    arguments = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "adapter_service.py"),
        "stop",
        "--codex-home",
        str(codex_home),
        "--manifest",
        str(manifest_path),
    ]
    if selected is not None:
        for provider_id in selected:
            arguments.extend(("--provider", provider_id))
    result = subprocess.run(
        arguments,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "cannot stop adapter services: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    if result.stdout.strip():
        print(result.stdout.strip())


def uninstall_custom(
    codex_home: Path,
    manifest_path: Path,
    dry_run: bool,
    *,
    stop_adapters: bool = True,
) -> int:
    resolved_manifest_path = manifest_path.expanduser().resolve()
    try:
        registry = read_install_registry(codex_home)
    except InstallError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    installation_id = manifest_installation_id(resolved_manifest_path)
    installations = registry["installations"]
    pending = registry["pending"]
    order = registry["order"]
    assert isinstance(installations, dict)
    assert isinstance(pending, dict)
    assert isinstance(order, list)
    active_record = installations.get(installation_id)
    pending_record = pending.get(installation_id)
    target_records = [
        record
        for record in (active_record, pending_record)
        if isinstance(record, dict)
    ]
    if not target_records:
        print(
            "error: no installation ownership record for manifest; "
            "preserving installed resources",
            file=sys.stderr,
        )
        return 2

    remaining_installations = {
        key: value for key, value in installations.items() if key != installation_id
    }
    remaining_pending = {
        key: value for key, value in pending.items() if key != installation_id
    }
    remaining_records = [
        *remaining_installations.values(),
        *remaining_pending.values(),
    ]
    remaining_order = [key for key in order if key != installation_id]
    target_adapter_providers = {
        provider_id
        for record in target_records
        for provider_id in record["adapter_providers"]
    }
    remaining_adapter_providers = {
        provider_id
        for other_record in remaining_records
        for provider_id in other_record["adapter_providers"]
    }
    shared_adapter_providers = target_adapter_providers & remaining_adapter_providers
    exclusive_adapter_providers = target_adapter_providers - remaining_adapter_providers
    if stop_adapters and target_adapter_providers and shared_adapter_providers:
        print(
            "preserved adapter services shared by another manifest: "
            + ", ".join(sorted(shared_adapter_providers))
        )
    if stop_adapters and exclusive_adapter_providers:
        try:
            load_manifest(resolved_manifest_path)
        except ValidationError as error:
            print(
                f"error: cannot stop adapter services without a readable manifest: {error}; "
                "rerun with --no-stop-adapters to remove recorded files only",
                file=sys.stderr,
            )
            return 2
        try:
            stop_adapter_services(
                codex_home,
                resolved_manifest_path,
                dry_run,
                exclusive_adapter_providers,
            )
        except RuntimeError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2

    remaining_files = {
        relative
        for other_record in remaining_records
        for relative in other_record["files"]
    }
    for record in target_records:
        for relative, digest in record["files"].items():
            path = codex_home / relative
            if relative in remaining_files:
                print(f"preserved (shared by another manifest): {path}")
                continue
            remove_file_if_digest(path, digest, dry_run)

    selection_path = codex_home / "models" / "subagent-selection.json"
    if isinstance(active_record, dict) and order and order[-1] == installation_id:
        expected_selection = recorded_selection_bytes(active_record)
        if selection_path.is_symlink():
            print(f"preserved (symlink): {selection_path}")
        elif selection_path.is_file() and selection_path.read_bytes() != expected_selection:
            print(f"preserved (modified): {selection_path}")
        elif selection_path.is_file() and remaining_order:
            previous_record = remaining_installations[remaining_order[-1]]
            restored_selection = recorded_selection_bytes(previous_record)
            if dry_run:
                print(f"would restore previous selection: {selection_path}")
            else:
                atomic_write(selection_path, restored_selection)
                print(f"restored previous selection: {selection_path}")
        elif selection_path.is_file():
            remove_file_if_unchanged(
                selection_path,
                expected_selection,
                dry_run,
            )

    legacy_agent = codex_home / "agents" / "deepseek-worker.toml"
    if legacy_agent.is_file() or remaining_records:
        print(f"preserved (shared by another installation): {SKILL_NAME} skill")
    else:
        uninstall_skills(codex_home, dry_run)

    remaining_provider_ids = {
        provider_id
        for other_record in remaining_records
        for provider_id in other_record["providers"]
    }
    for record in target_records:
        for provider_id, provider_block in reversed(tuple(record["providers"].items())):
            if provider_id in remaining_provider_ids:
                print(
                    f"preserved (shared by another manifest): "
                    f"{codex_home / 'config.toml'} [{provider_id}]"
                )
                continue
            status = uninstall_provider_block(codex_home, provider_block, dry_run)
            print(f"{status}: {codex_home / 'config.toml'} [{provider_id}]")

    if not dry_run:
        write_install_registry(
            codex_home,
            {
                "version": INSTALL_REGISTRY_VERSION,
                "installations": remaining_installations,
                "pending": remaining_pending,
                "order": remaining_order,
            },
        )
    return 0


def main() -> int:
    args = parse_args()
    codex_home = args.codex_home.expanduser().resolve()
    print(f"codex home: {codex_home}")
    if args.manifest is not None:
        return uninstall_custom(
            codex_home,
            args.manifest,
            args.dry_run,
            stop_adapters=not args.no_stop_adapters,
        )

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
    if custom_agent_paths(codex_home):
        print(f"preserved (shared by another installation): {SKILL_NAME} skill")
    else:
        uninstall_skills(codex_home, args.dry_run)
    status = uninstall_provider(codex_home, args.dry_run)
    print(f"{status}: {codex_home / 'config.toml'}")

    print(f"next: reinstall with {sys.executable} scripts/install.py; config backups keep rollback copies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
