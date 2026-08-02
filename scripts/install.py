#!/usr/bin/env python3
"""Install custom subagent providers, workers, model catalogs, and the delegation skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from model_manifest import (
    ModelManifest,
    catalog_filename,
    load_manifest,
    render_agent,
    render_model_catalog,
    render_provider,
    unsupported_protocols,
)
from model_selection import ValidationError
from platform_runtime import python_command, python_command_toml, toml_path_escape


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROVIDER_HEADER = "[model_providers.deepseek]"
SKILL_MANIFEST = ".codex-deepseek-manifest.json"
INSTALL_REGISTRY = ".codex-subagent-installations.json"
INSTALL_REGISTRY_VERSION = 1


class InstallError(Exception):
    """Refuse to continue when the install target violates the symlink policy."""


def resolve_codex_home() -> Path:
    """Return the effective Codex home, falling back when CODEX_HOME is blank."""
    raw = os.environ.get("CODEX_HOME")
    if raw is None or not raw.strip():
        return Path.home() / ".codex"
    return Path(raw)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install Codex Custom Subagents using a manifest or the legacy DeepSeek profile."
    )
    parser.add_argument("--codex-home", type=Path, default=resolve_codex_home())
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="install a custom multi-model manifest instead of the built-in DeepSeek profile",
    )
    parser.add_argument(
        "--no-start-adapters",
        action="store_true",
        help="install adapter files without creating persistent LaunchAgents",
    )
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


def atomic_write(destination: Path, content: bytes, mode: int | None = None) -> None:
    """Write one file by replacing it only after a complete same-directory write."""
    ensure_not_symlink(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.read_bytes() == content:
        return

    if destination.exists():
        mode = stat.S_IMODE(destination.stat().st_mode)
    elif mode is None:
        mode = 0o644
    assert mode is not None

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary.chmod(mode)
        ensure_not_symlink(destination)
        backup_if_changed(destination, content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def install_file(
    source: Path,
    destination: Path,
    replacements: dict[str, str] | None = None,
) -> None:
    text = source.read_text(encoding="utf-8")
    for old, new in (replacements or {}).items():
        text = text.replace(old, new)
    content = text.encode("utf-8")
    atomic_write(destination, content, stat.S_IMODE(source.stat().st_mode))
    print(f"installed: {destination}")


def install_content(content: bytes, destination: Path) -> None:
    atomic_write(destination, content)
    print(f"installed: {destination}")


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def content_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def install_registry_path(codex_home: Path) -> Path:
    return codex_home / INSTALL_REGISTRY


def empty_install_registry() -> dict[str, object]:
    return {
        "version": INSTALL_REGISTRY_VERSION,
        "installations": {},
        "pending": {},
        "order": [],
    }


def _valid_managed_relative_path(value: str) -> bool:
    path = Path(value)
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts


def _valid_install_record(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    providers = record.get("providers")
    provider_fingerprints = record.get("provider_fingerprints")
    files = record.get("files")
    selection = record.get("selection")
    adapter_providers = record.get("adapter_providers")
    if not isinstance(record.get("manifest_path"), str):
        return False
    if not isinstance(providers, dict) or not all(
        isinstance(provider_id, str) and isinstance(block, str)
        for provider_id, block in providers.items()
    ):
        return False
    if not isinstance(provider_fingerprints, dict) or not all(
        isinstance(provider_id, str)
        and isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
        for provider_id, digest in provider_fingerprints.items()
    ):
        return False
    if set(provider_fingerprints) != set(providers):
        return False
    if not isinstance(files, dict) or not all(
        isinstance(relative, str)
        and _valid_managed_relative_path(relative)
        and isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
        for relative, digest in files.items()
    ):
        return False
    if not isinstance(selection, dict):
        return False
    if not isinstance(adapter_providers, list) or not all(
        isinstance(provider_id, str) for provider_id in adapter_providers
    ):
        return False
    return len(adapter_providers) == len(set(adapter_providers))


def read_install_registry(codex_home: Path) -> dict[str, object]:
    path = install_registry_path(codex_home)
    ensure_not_symlink(path)
    if not path.exists():
        return empty_install_registry()
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InstallError(f"cannot read installation ownership registry {path}: {error}") from error
    if not isinstance(registry, dict) or registry.get("version") != INSTALL_REGISTRY_VERSION:
        raise InstallError(f"invalid installation ownership registry: {path}")
    installations = registry.get("installations")
    pending = registry.get("pending", {})
    order = registry.get("order")
    if (
        not isinstance(installations, dict)
        or not all(
            isinstance(installation_id, str) and _valid_install_record(record)
            for installation_id, record in installations.items()
        )
        or not isinstance(order, list)
        or not all(isinstance(installation_id, str) for installation_id in order)
        or len(order) != len(set(order))
        or set(order) != set(installations)
        or not isinstance(pending, dict)
        or not all(
            isinstance(installation_id, str) and _valid_install_record(record)
            for installation_id, record in pending.items()
        )
    ):
        raise InstallError(f"invalid installation ownership registry: {path}")
    registry["pending"] = pending
    return registry


def write_install_registry(codex_home: Path, registry: dict[str, object]) -> None:
    content = (json.dumps(registry, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write(install_registry_path(codex_home), content)


def manifest_installation_id(manifest_path: Path) -> str:
    resolved = str(manifest_path.expanduser().resolve()).encode("utf-8")
    return hashlib.sha256(resolved).hexdigest()


def custom_installation_record(
    codex_home: Path,
    manifest_path: Path,
    manifest: ModelManifest,
    template: str,
) -> dict[str, object]:
    files: dict[str, str] = {}
    credential_command = (
        sys.executable,
        str(codex_home / "helpers" / "credential_store.py"),
    )

    def add_file(path: Path, content: bytes) -> None:
        relative = path.relative_to(codex_home).as_posix()
        files[relative] = content_digest(content)

    for model in manifest.models.values():
        add_file(
            codex_home / "agents" / f"{model.agent}.toml",
            render_agent(template, codex_home, model).encode("utf-8"),
        )
        add_file(
            codex_home / "models" / catalog_filename(model),
            render_model_catalog(model),
        )
    adapter_providers = [
        provider.id
        for provider in manifest.providers.values()
        if provider.adapter is not None
    ]
    if adapter_providers:
        for filename in ("anthropic_adapter_protocol.py", "anthropic_responses_adapter.py", "service_runner.py"):
            add_file(
                codex_home / "adapters" / filename,
                (PROJECT_ROOT / "scripts" / filename).read_bytes(),
            )
    if any(provider.auth.kind == "keychain" for provider in manifest.providers.values()):
        add_file(
            codex_home / "helpers" / "credential_store.py",
            (PROJECT_ROOT / "scripts" / "credential_store.py").read_bytes(),
        )
    return {
        "manifest_path": str(manifest_path),
        "providers": {
            provider.id: render_provider(provider, credential_command)
            for provider in manifest.providers.values()
        },
        "provider_fingerprints": {
            provider.id: content_digest(
                json.dumps(asdict(provider), sort_keys=True).encode("utf-8")
            )
            for provider in manifest.providers.values()
        },
        "files": files,
        "selection": manifest.normalized_selection(),
        "adapter_providers": adapter_providers,
    }


def validate_installation_ownership(
    registry: dict[str, object],
    installation_id: str,
    record: dict[str, object],
) -> None:
    installations = registry["installations"]
    pending = registry["pending"]
    assert isinstance(installations, dict)
    assert isinstance(pending, dict)
    if installation_id in pending:
        raise InstallError(
            "a pending installation already exists for this manifest; "
            "uninstall it before retrying"
        )
    files = record["files"]
    providers = record["providers"]
    provider_fingerprints = record["provider_fingerprints"]
    assert isinstance(files, dict)
    assert isinstance(providers, dict)
    assert isinstance(provider_fingerprints, dict)
    owners = [*installations.items(), *pending.items()]
    for other_id, other_record in owners:
        if other_id == installation_id:
            continue
        assert isinstance(other_record, dict)
        other_files = other_record["files"]
        other_providers = other_record["providers"]
        other_provider_fingerprints = other_record["provider_fingerprints"]
        assert isinstance(other_files, dict)
        assert isinstance(other_providers, dict)
        assert isinstance(other_provider_fingerprints, dict)
        for relative in files.keys() & other_files.keys():
            if files[relative] != other_files[relative]:
                raise InstallError(
                    f"managed file {relative} conflicts with installed manifest "
                    f"{other_record['manifest_path']}"
                )
        for provider_id in providers.keys() & other_providers.keys():
            if (
                provider_fingerprints[provider_id]
                != other_provider_fingerprints[provider_id]
            ):
                raise InstallError(
                    f"provider {provider_id} conflicts with installed manifest "
                    f"{other_record['manifest_path']}"
                )


def stage_manifest_installation(
    codex_home: Path,
    registry: dict[str, object],
    installation_id: str,
    record: dict[str, object],
) -> dict[str, object]:
    pending = dict(registry["pending"])
    if installation_id in pending:
        raise InstallError(
            "a pending installation already exists for this manifest; "
            "uninstall it before retrying"
        )
    pending[installation_id] = record
    staged = {
        "version": INSTALL_REGISTRY_VERSION,
        "installations": dict(registry["installations"]),
        "pending": pending,
        "order": list(registry["order"]),
    }
    write_install_registry(codex_home, staged)
    return staged


def activate_manifest_installation(
    codex_home: Path,
    registry: dict[str, object],
    installation_id: str,
) -> None:
    installations = dict(registry["installations"])
    pending = dict(registry["pending"])
    record = pending.pop(installation_id)
    order = [item for item in registry["order"] if item != installation_id]
    installations[installation_id] = record
    order.append(installation_id)
    write_install_registry(
        codex_home,
        {
            "version": INSTALL_REGISTRY_VERSION,
            "installations": installations,
            "pending": pending,
            "order": order,
        },
    )


def preflight_selection_target(path: Path) -> bytes | None:
    ensure_not_symlink(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise InstallError(f"selection target must be a regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as error:
        raise InstallError(f"cannot read existing selection {path}: {error}") from error


def rollback_selection(
    path: Path,
    previous_content: bytes | None,
    installed_content: bytes,
) -> None:
    ensure_not_symlink(path)
    if previous_content is not None:
        atomic_write(path, previous_content)
        return
    if not path.exists():
        return
    if not path.is_file() or path.read_bytes() != installed_content:
        raise InstallError(f"selection changed during registry rollback: {path}")
    path.unlink()


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
    atomic_write(manifest_path, skill_manifest_bytes(source))
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
    atomic_write(config_path, content)
    print(f"updated: {config_path}")


def validate_provider_blocks(codex_home: Path, blocks: list[tuple[str, str]]) -> None:
    config_path = codex_home / "config.toml"
    ensure_not_symlink(config_path)
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    for provider_id, block in blocks:
        header = f"[model_providers.{provider_id}]"
        if toml_header_present(existing, header):
            if block.strip() in existing:
                continue
            raise InstallError(
                f"existing provider {provider_id} differs from the manifest; "
                "remove or reconcile that provider block before reinstalling"
            )


def install_provider_blocks(codex_home: Path, blocks: list[tuple[str, str]]) -> None:
    validate_provider_blocks(codex_home, blocks)
    config_path = codex_home / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    additions = []
    for provider_id, block in blocks:
        header = f"[model_providers.{provider_id}]"
        if toml_header_present(existing, header):
            print(f"preserved: existing {provider_id} provider configuration")
            continue
        additions.append(block.strip())
    if not additions:
        return
    separator = "\n\n" if existing.strip() else ""
    joined = "\n\n".join(additions)
    content = f"{existing.rstrip()}{separator}{joined}\n".encode("utf-8")
    atomic_write(config_path, content)
    print(f"updated: {config_path}")


def start_adapter_services(codex_home: Path, manifest_path: Path) -> None:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "adapter_service.py"),
        "start",
        "--codex-home",
        str(codex_home),
        "--manifest",
        str(manifest_path),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise InstallError(
            "adapter service installation failed: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    if result.stdout.strip():
        print(result.stdout.strip())


def install_custom_manifest(
    codex_home: Path,
    manifest_path: Path,
    *,
    start_adapters: bool = True,
) -> None:
    resolved_manifest_path = manifest_path.expanduser().resolve()
    manifest = load_manifest(resolved_manifest_path)
    unsupported = unsupported_protocols(manifest)
    if unsupported:
        raise InstallError(
            "current Codex runtime only supports wire_api='responses'; unsupported providers: "
            + ", ".join(unsupported)
        )
    template = (PROJECT_ROOT / "agents" / "model-worker.toml.template").read_text(
        encoding="utf-8"
    )
    installation_id = manifest_installation_id(resolved_manifest_path)
    installation_record = custom_installation_record(
        codex_home,
        resolved_manifest_path,
        manifest,
        template,
    )
    registry = read_install_registry(codex_home)
    validate_installation_ownership(registry, installation_id, installation_record)
    selection_path = codex_home / "models" / "subagent-selection.json"
    previous_selection = preflight_selection_target(selection_path)
    credential_command = (
        sys.executable,
        str(codex_home / "helpers" / "credential_store.py"),
    )

    provider_blocks = [
        (provider.id, render_provider(provider, credential_command))
        for provider in manifest.providers.values()
    ]
    adapter_providers = [
        provider for provider in manifest.providers.values() if provider.adapter
    ]
    adapter_catalogs = {}
    for provider in adapter_providers:
        catalogs = [
            model
            for model in manifest.models.values()
            if model.provider_id == provider.id
        ]
        if len(catalogs) != 1:
            raise InstallError(
                f"adapter provider {provider.id} requires exactly one model catalog; "
                f"found {len(catalogs)}"
            )
        adapter_catalogs[provider.id] = catalogs[0]

    validate_provider_blocks(codex_home, provider_blocks)
    staged_registry = stage_manifest_installation(
        codex_home,
        registry,
        installation_id,
        installation_record,
    )
    install_skill(codex_home)
    if any(provider.auth.kind == "keychain" for provider in manifest.providers.values()):
        install_content(
            (PROJECT_ROOT / "scripts" / "credential_store.py").read_bytes(),
            codex_home / "helpers" / "credential_store.py",
        )
    if adapter_providers:
        for filename in ("anthropic_adapter_protocol.py", "anthropic_responses_adapter.py", "service_runner.py"):
            source = PROJECT_ROOT / "scripts" / filename
            install_content(source.read_bytes(), codex_home / "adapters" / filename)
    for model in manifest.models.values():
        rendered = render_agent(template, codex_home, model).encode("utf-8")
        install_content(rendered, codex_home / "agents" / f"{model.agent}.toml")
        install_content(
            render_model_catalog(model),
            codex_home / "models" / catalog_filename(model),
        )
    install_provider_blocks(codex_home, provider_blocks)
    selection = (
        json.dumps(manifest.normalized_selection(), indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if adapter_providers and start_adapters:
        start_adapter_services(codex_home, resolved_manifest_path)
    elif adapter_providers:
        for provider in adapter_providers:
            adapter = provider.adapter
            assert adapter is not None
            catalog = adapter_catalogs[provider.id]
            print(
                f"start adapter: {python_command()} "
                f"{codex_home / 'adapters' / 'anthropic_responses_adapter.py'} "
                f"--listen {adapter.listen_host} --port {adapter.listen_port} "
                f"--service-id {provider.id} "
                f"--max-output-tokens {adapter.max_output_tokens} "
                f"--upstream-base-url {provider.base_url} "
                f"--model-catalog {codex_home / 'models' / catalog_filename(catalog)}"
            )
    atomic_write(selection_path, selection)
    try:
        activate_manifest_installation(
            codex_home,
            staged_registry,
            installation_id,
        )
    except Exception as error:
        try:
            rollback_selection(selection_path, previous_selection, selection)
        except (InstallError, OSError) as rollback_error:
            raise InstallError(
                f"installation registry activation failed ({error}); "
                f"selection rollback also failed: {rollback_error}"
            ) from error
        raise InstallError(f"installation registry activation failed: {error}") from error
    print(f"installed: {selection_path}")


def main() -> int:
    args = parse_args()
    codex_home = args.codex_home.expanduser().resolve()
    try:
        codex_home.mkdir(parents=True, exist_ok=True)
        if args.manifest is not None:
            install_custom_manifest(
                codex_home,
                args.manifest,
                start_adapters=not args.no_start_adapters,
            )
            print(
                "next: store credentials in their configured secure source, run "
                "scripts/doctor.py with the same --manifest, then start a new Codex task"
            )
            return 0
        install_skill(codex_home)
        install_file(
            PROJECT_ROOT / "agents" / "deepseek-worker.toml.template",
            codex_home / "agents" / "deepseek-worker.toml",
            {
                "__CODEX_HOME__": toml_path_escape(str(codex_home)),
                "__PYTHON_COMMAND__": python_command_toml(),
            },
        )
        install_file(
            PROJECT_ROOT / "models" / "deepseek-v4-flash.json",
            codex_home / "models" / "deepseek-v4-flash.json",
        )
        install_provider(codex_home)
    except (InstallError, ValidationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print("next: store the API key in macOS Keychain, run scripts/doctor.py, then start a new Codex task")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
