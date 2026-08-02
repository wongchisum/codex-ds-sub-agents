#!/usr/bin/env python3
"""Migrate managed predecessor skills to `codex-custom-subagents`.

Detects an old managed install (recognized by its own
`.codex-deepseek-manifest.json` ownership record), removes only files whose
content is unchanged since that install, preserves modified or unknown files,
then installs the current skill and re-renders the worker agent so it points
at the new skill path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from install import (
    LEGACY_SKILL_NAMES,
    InstallError,
    atomic_write,
    file_digest,
    install_legacy_profile,
    install_skill,
    read_install_registry,
    remove_managed_legacy_skills,
    resolve_codex_home,
    write_install_registry,
)
from model_manifest import load_manifest, render_agent
from model_selection import ValidationError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate managed deepseek-delegation or codex-custom-agents skill installs "
            "to codex-custom-subagents, then install the current plugin resources."
        )
    )
    parser.add_argument("--codex-home", type=Path, default=resolve_codex_home())
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="detect the legacy install and preview what would be removed without changing anything",
    )
    return parser.parse_args()


def rerender_registered_agents(
    codex_home: Path,
    registry: dict[str, object],
) -> int:
    template = (
        Path(__file__).resolve().parents[1] / "agents" / "model-worker.toml.template"
    ).read_text(encoding="utf-8")
    installations = registry["installations"]
    count = 0
    for installation_id in registry["order"]:
        record = installations[installation_id]
        manifest = load_manifest(Path(record["manifest_path"]))
        files = record["files"]
        for model in manifest.models.values():
            destination = codex_home / "agents" / f"{model.agent}.toml"
            atomic_write(
                destination,
                render_agent(template, codex_home, model).encode("utf-8"),
            )
            relative = str(destination.relative_to(codex_home))
            files[relative] = file_digest(destination)
        count += 1
    write_install_registry(codex_home, registry)
    return count


def main() -> int:
    args = parse_args()
    codex_home = args.codex_home.expanduser().resolve()
    try:
        codex_home.mkdir(parents=True, exist_ok=True)
        registry = read_install_registry(codex_home)
        if registry["pending"]:
            raise InstallError(
                "cannot migrate while the installation registry contains pending records; "
                "finish or uninstall the failed installation first"
            )
        installations = registry["installations"]
        manifest_paths = [
            Path(installations[installation_id]["manifest_path"])
            for installation_id in registry["order"]
        ]
        missing = [path for path in manifest_paths if not path.is_file()]
        if missing:
            raise InstallError(
                "cannot re-render installed agents because these manifests are missing: "
                + ", ".join(str(path) for path in missing)
            )
        legacy = remove_managed_legacy_skills(codex_home, dry_run=args.dry_run)
        if not legacy:
            print(
                "no managed legacy skill install found: "
                + ", ".join(LEGACY_SKILL_NAMES)
            )
        if args.dry_run:
            if manifest_paths:
                print(f"would re-render {len(manifest_paths)} registered manifest installation(s)")
            print("dry run: no changes were made")
            return 0
        if manifest_paths:
            install_skill(codex_home)
            rerender_registered_agents(codex_home, registry)
        else:
            install_legacy_profile(codex_home)
    except (InstallError, ValidationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(
        "migration complete: legacy owned files removed, new skill installed, "
        f"{len(manifest_paths)} registered manifest installation(s) re-rendered"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
