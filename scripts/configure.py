#!/usr/bin/env python3
"""Configure, install, and verify Codex Custom Subagents in one workflow."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from install import atomic_write, resolve_codex_home
from credential_store import credential_exists, validate_identity
from diagnostics import redact_text
from model_manifest import ModelManifest, load_manifest
from model_selection import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANAGED_MANIFEST_DIR = Path("custom-subagents") / "manifests"
PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
KEYCHAIN_TIMEOUT_SECONDS = 5.0
NEEDS_CREDENTIAL_EXIT = 3
MAX_LOG_OUTPUT_CHARS = 65536


@dataclass(frozen=True)
class Profile:
    name: str
    manifest: Optional[Path]
    description: str


@dataclass(frozen=True)
class ModelProtocolPreset:
    name: str
    manifest: Path
    label: str


@dataclass(frozen=True)
class Protocol:
    name: str
    description: str
    transport: str


@dataclass(frozen=True)
class MissingCredential:
    kind: str
    name: str
    account: str = "codex"


PROFILES: tuple[Profile, ...] = (
    Profile(
        "deepseek-anthropic",
        PROJECT_ROOT / "config" / "deepseek-anthropic-1m.example.json",
        "DeepSeek V4 Flash through the Anthropic Messages adapter",
    ),
    Profile(
        "gemini-anthropic",
        PROJECT_ROOT / "config" / "gemini-anthropic.example.json",
        "Gemini 3.5 Flash through the Anthropic Messages adapter",
    ),
    Profile(
        "claude-gemini",
        PROJECT_ROOT / "config" / "model-providers.example.json",
        "Claude Opus 4.6 primary with Gemini 3.5 Flash fallback",
    ),
    Profile(
        "legacy-deepseek",
        None,
        "Legacy fixed DeepSeek Responses profile",
    ),
)
PROFILE_BY_NAME = {profile.name: profile for profile in PROFILES}
MODEL_PROTOCOL_PRESETS: tuple[ModelProtocolPreset, ...] = (
    ModelProtocolPreset(
        "deepseek-anthropic",
        PROJECT_ROOT / "config" / "deepseek-anthropic-1m.example.json",
        "deepseek (anthropic)",
    ),
    ModelProtocolPreset(
        "deepseek-openai",
        PROJECT_ROOT / "config" / "deepseek-openai.example.json",
        "deepseek (openai)",
    ),
    ModelProtocolPreset(
        "gemini-anthropic",
        PROJECT_ROOT / "config" / "gemini-anthropic.example.json",
        "gemini (anthropic)",
    ),
    ModelProtocolPreset(
        "claude-anthropic",
        PROJECT_ROOT / "config" / "claude-anthropic.example.json",
        "claude (anthropic)",
    ),
)
PRESET_BY_NAME = {preset.name: preset for preset in MODEL_PROTOCOL_PRESETS}
PROTOCOLS: tuple[Protocol, ...] = (
    Protocol(
        "openai_responses",
        "Upstream implements the OpenAI Responses wire protocol; no local adapter",
        "direct",
    ),
    Protocol(
        "anthropic_messages",
        "Upstream implements Anthropic Messages; installer manages a local Responses adapter",
        "local_adapter",
    ),
)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--profile", choices=tuple(PROFILE_BY_NAME))
    source.add_argument("--manifest", type=Path, help="install a custom manifest")
    source.add_argument(
        "--primary",
        choices=tuple(PRESET_BY_NAME),
        help="primary model-protocol preset",
    )
    source.add_argument("--list-profiles", action="store_true")
    source.add_argument("--list-protocols", action="store_true")
    source.add_argument("--list-model-protocols", action="store_true")
    parser.add_argument(
        "--fallback",
        action="append",
        default=[],
        choices=tuple(PRESET_BY_NAME),
        help="ordered fallback preset; repeat to declare more than one",
    )
    parser.add_argument(
        "--name",
        help="stable local name for a custom manifest; defaults to its filename stem",
    )
    parser.add_argument("--codex-home", type=Path, default=resolve_codex_home())
    parser.add_argument(
        "--no-start-adapters",
        action="store_true",
        help="install files without starting persistent adapter services",
    )
    parser.add_argument(
        "--skip-doctor",
        action="store_true",
        help="stop after installation without running doctor",
    )
    parser.add_argument(
        "--skip-credential-check",
        action="store_true",
        help="offline-only: do not require configured Keychain or environment credentials",
    )
    parser.add_argument(
        "--skip-adapter-health",
        action="store_true",
        help="offline-only: ask doctor not to probe adapter health",
    )
    return parser.parse_args(argv)


def print_profiles() -> None:
    print("Compatibility example profiles (new configuration should use a schema v2 manifest):")
    for profile in PROFILES:
        print(f"{profile.name:20} {profile.description}")


def print_model_protocols() -> None:
    print("Model-protocol presets:")
    for preset in MODEL_PROTOCOL_PRESETS:
        print(f"{preset.name:20} {preset.label}")
    print("Fallbacks are configured separately with repeated --fallback options.")


def print_protocols() -> None:
    for protocol in PROTOCOLS:
        print(f"{protocol.name:20} {protocol.description}")


def choose_profile(input_fn: Callable[[str], str] = input) -> Profile:
    print("Available profiles:")
    for index, profile in enumerate(PROFILES, 1):
        print(f"  {index}. {profile.name} — {profile.description}")
    raw = input_fn("Select a profile number: ").strip()
    try:
        index = int(raw)
    except ValueError as error:
        raise ValueError("profile selection must be a number") from error
    if not 1 <= index <= len(PROFILES):
        raise ValueError(f"profile selection must be between 1 and {len(PROFILES)}")
    return PROFILES[index - 1]


def choose_model_protocols(
    input_fn: Callable[[str], str] = input,
) -> tuple[str, tuple[str, ...]]:
    print("Available model-protocol presets:")
    for index, preset in enumerate(MODEL_PROTOCOL_PRESETS, 1):
        print(f"  {index}. {preset.label}")
    primary_raw = input_fn("Select the primary model number: ").strip()
    try:
        primary_index = int(primary_raw)
    except ValueError as error:
        raise ValueError("primary selection must be a number") from error
    if not 1 <= primary_index <= len(MODEL_PROTOCOL_PRESETS):
        raise ValueError(
            f"primary selection must be between 1 and {len(MODEL_PROTOCOL_PRESETS)}"
        )
    fallback_raw = input_fn(
        "Optional fallback numbers in order (comma-separated, blank for none): "
    ).strip()
    fallback_names: list[str] = []
    if fallback_raw:
        try:
            fallback_indexes = [int(value.strip()) for value in fallback_raw.split(",")]
        except ValueError as error:
            raise ValueError("fallback selections must be comma-separated numbers") from error
        for index in fallback_indexes:
            if not 1 <= index <= len(MODEL_PROTOCOL_PRESETS):
                raise ValueError(
                    f"fallback selection must be between 1 and {len(MODEL_PROTOCOL_PRESETS)}"
                )
            fallback_names.append(MODEL_PROTOCOL_PRESETS[index - 1].name)
    primary = MODEL_PROTOCOL_PRESETS[primary_index - 1].name
    validate_preset_chain(primary, fallback_names)
    return primary, tuple(fallback_names)


def validate_preset_chain(primary: str, fallbacks: Sequence[str]) -> None:
    chain = [primary, *fallbacks]
    duplicates = sorted({name for name in chain if chain.count(name) > 1})
    if duplicates:
        raise ValueError(
            "primary and fallbacks must be unique; duplicate: " + ", ".join(duplicates)
        )


def build_preset_manifest(primary: str, fallbacks: Sequence[str]) -> bytes:
    validate_preset_chain(primary, fallbacks)
    providers: list[object] = []
    models: list[object] = []
    model_ids: list[str] = []
    for name in (primary, *fallbacks):
        raw = json.loads(PRESET_BY_NAME[name].manifest.read_text(encoding="utf-8"))
        providers.extend(raw["providers"])
        models.extend(raw["models"])
        model_ids.append(raw["selection"]["primary"])
    document = {
        "schema_version": 2,
        "selection": {
            "primary": model_ids[0],
            "fallbacks": model_ids[1:],
            "max_switches": len(model_ids) - 1,
        },
        "providers": providers,
        "models": models,
    }
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def install_preset_manifest(
    primary: str,
    fallbacks: Sequence[str],
    codex_home: Path,
    name: str,
) -> tuple[Path, ModelManifest]:
    content = build_preset_manifest(primary, fallbacks)
    destination = managed_manifest_path(codex_home, validate_local_name(name))
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise ValueError(f"managed manifest target must be a regular file: {destination}")
    if destination.is_file() and destination.read_bytes() != content:
        raise ValueError(
            f"managed manifest {destination} already has different content; "
            "use a new --name or uninstall the existing profile first"
        )
    if not destination.exists():
        atomic_write(destination, content, mode=0o600)
        print(f"configured manifest: {destination}", flush=True)
    return destination, load_manifest(destination)


def validate_local_name(value: str) -> str:
    normalized = value.strip().lower().replace(".", "-").replace(" ", "-")
    if not PROFILE_NAME.fullmatch(normalized):
        raise ValueError("manifest name must match [a-z0-9][a-z0-9_-]{0,63}")
    return normalized


def managed_manifest_path(codex_home: Path, name: str) -> Path:
    return codex_home / MANAGED_MANIFEST_DIR / f"{name}.json"


def configure_log_path(codex_home: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return codex_home / "logs" / "custom-subagents" / f"configure-{stamp}-{os.getpid()}.jsonl"


def log_event(path: Path, event: str, **fields: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event": event,
        **fields,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")
    if os.name != "nt":
        path.chmod(0o600)


def install_managed_manifest(source: Path, codex_home: Path, name: str) -> tuple[Path, ModelManifest]:
    resolved = source.expanduser().resolve()
    manifest = load_manifest(resolved)
    destination = managed_manifest_path(codex_home, validate_local_name(name))
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise ValueError(f"managed manifest target must be a regular file: {destination}")
    if destination.resolve() != resolved:
        content = resolved.read_bytes()
        if destination.is_file() and destination.read_bytes() != content:
            raise ValueError(
                f"managed manifest {destination} already has different content; "
                "use a new --name or uninstall the existing profile first"
            )
        if not destination.exists():
            atomic_write(destination, content, mode=0o600)
            print(f"configured manifest: {destination}", flush=True)
    if os.name != "nt":
        destination.chmod(0o600)
    return destination, manifest


def keychain_exists(
    account: str,
    service: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    if platform.system() == "Darwin":
        security = Path("/usr/bin/security")
        if not security.is_file():
            return False
        try:
            result = runner(
                [str(security), "find-generic-password", "-a", account, "-s", service],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0
    return credential_exists(validate_identity(service, account))


def missing_credentials(
    manifest: ModelManifest,
    *,
    environ: Mapping[str, str] = os.environ,
    keychain_check: Callable[[str, str], bool] = keychain_exists,
) -> tuple[MissingCredential, ...]:
    missing: list[MissingCredential] = []
    seen: set[tuple[str, str, str]] = set()
    for provider in manifest.providers.values():
        auth = provider.auth
        identity = (auth.kind, auth.name, auth.account)
        if identity in seen:
            continue
        seen.add(identity)
        if auth.kind == "keychain":
            available = keychain_check(auth.account, auth.name)
        else:
            available = bool(environ.get(auth.name))
        if not available:
            missing.append(MissingCredential(auth.kind, auth.name, auth.account))
    return tuple(missing)


def print_credential_instructions(missing: Sequence[MissingCredential]) -> None:
    print("\nCredentials are not stored by this project. Complete these steps locally:")
    for credential in missing:
        if credential.kind == "keychain":
            if platform.system() == "Windows":
                print(
                    f'python "{PROJECT_ROOT / "scripts" / "credential_store.py"}" set '
                    f"--account {shlex.quote(credential.account)} "
                    f"--service {shlex.quote(credential.name)}"
                )
            else:
                print(
                    "/usr/bin/security add-generic-password -U "
                    f"-a {shlex.quote(credential.account)} "
                    f"-s {shlex.quote(credential.name)} -w"
                )
        else:
            print(
                "Set the environment variable named "
                f"{credential.name!r} in the adapter service environment."
            )
    print("Do not paste API keys into a Codex prompt. Re-run the same configure command afterward.")


def run_command(
    command: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    log_path: Optional[Path] = None,
    phase: str = "command",
) -> int:
    result = runner(
        list(command),
        check=False,
        text=True,
        capture_output=True,
    )
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if stdout:
        print(stdout, end="" if stdout.endswith("\n") else "\n")
    if stderr:
        print(stderr, end="" if stderr.endswith("\n") else "\n", file=sys.stderr)
    if log_path is not None:
        log_event(
            log_path,
            "phase_finished",
            phase=phase,
            exit_code=result.returncode,
            command=[Path(part).name if index in (0, 1) else part for index, part in enumerate(command)],
            stdout=redact_text(stdout[-MAX_LOG_OUTPUT_CHARS:]),
            stderr=redact_text(stderr[-MAX_LOG_OUTPUT_CHARS:]),
            output_truncated=len(stdout) > MAX_LOG_OUTPUT_CHARS or len(stderr) > MAX_LOG_OUTPUT_CHARS,
        )
    return result.returncode


def install_command(
    codex_home: Path,
    manifest: Optional[Path],
    no_start_adapters: bool,
) -> list[str]:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "install.py"),
        "--codex-home",
        str(codex_home),
    ]
    if manifest is not None:
        command.extend(("--manifest", str(manifest)))
    if no_start_adapters:
        command.append("--no-start-adapters")
    return command


def doctor_command(
    codex_home: Path,
    manifest: Optional[Path],
    *,
    skip_credential_check: bool,
    skip_adapter_health: bool,
) -> list[str]:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "doctor.py"),
        "--codex-home",
        str(codex_home),
    ]
    if manifest is not None:
        command.extend(("--manifest", str(manifest)))
    if skip_credential_check:
        command.append("--skip-keychain")
    if skip_adapter_health and manifest is not None:
        command.append("--skip-adapter-health")
    return command


def legacy_missing_credentials() -> tuple[MissingCredential, ...]:
    if keychain_exists("codex", "deepseek-api-key"):
        return ()
    return (MissingCredential("keychain", "deepseek-api-key", "codex"),)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.list_protocols:
        print_protocols()
        return 0
    if args.list_profiles:
        print_profiles()
        return 0
    if args.list_model_protocols:
        print_model_protocols()
        return 0
    if args.fallback and not args.primary:
        print("error: --fallback requires --primary", file=sys.stderr)
        return 2

    try:
        primary = args.primary
        fallbacks = tuple(args.fallback)
        if args.profile:
            profile = PROFILE_BY_NAME[args.profile]
        elif args.manifest is None and primary is None:
            if not sys.stdin.isatty():
                print(
                    "error: use --primary, --profile, or --manifest when stdin is not interactive",
                    file=sys.stderr,
                )
                return 2
            primary, fallbacks = choose_model_protocols()
            profile = None
        else:
            profile = None

        codex_home = args.codex_home.expanduser().resolve()
        codex_home.mkdir(parents=True, exist_ok=True)
        log_path = configure_log_path(codex_home)
        log_event(log_path, "configure_started", platform=platform.system())
        manifest_path: Optional[Path]
        manifest: Optional[ModelManifest]
        if primary is not None:
            manifest_path, manifest = install_preset_manifest(
                primary,
                fallbacks,
                codex_home,
                args.name or primary,
            )
        elif profile is not None and profile.manifest is None:
            manifest_path = None
            manifest = None
        else:
            source = args.manifest if profile is None else profile.manifest
            assert source is not None
            default_name = source.stem if profile is None else profile.name
            manifest_path, manifest = install_managed_manifest(
                source,
                codex_home,
                args.name or default_name,
            )
    except (OSError, ValueError, ValidationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if not args.skip_credential_check:
        missing = legacy_missing_credentials() if manifest is None else missing_credentials(manifest)
        if missing:
            log_event(
                log_path,
                "credential_check_failed",
                missing=[{"kind": item.kind, "name": item.name, "account": item.account} for item in missing],
            )
            print_credential_instructions(missing)
            print(f"Diagnostic log: {log_path}")
            return NEEDS_CREDENTIAL_EXIT

    print("\nInstalling configured subagent resources...", flush=True)
    installed = run_command(
        install_command(codex_home, manifest_path, args.no_start_adapters),
        log_path=log_path,
        phase="install",
    )
    if installed != 0:
        print(f"Diagnostic log: {log_path}", file=sys.stderr)
        return installed

    if args.skip_doctor:
        print("installation completed; doctor was skipped")
        return 0

    print("\nRunning installation doctor...", flush=True)
    checked = run_command(
        doctor_command(
            codex_home,
            manifest_path,
            skip_credential_check=args.skip_credential_check,
            skip_adapter_health=args.skip_adapter_health,
        ),
        log_path=log_path,
        phase="doctor",
    )
    if checked != 0:
        print(f"Diagnostic log: {log_path}", file=sys.stderr)
        return checked

    log_event(log_path, "configure_completed", exit_code=0)
    print("\nSetup completed. Start a new Codex task so Desktop loads the installed agent types.")
    print(f"Diagnostic log: {log_path}")
    print("Then use $codex-custom-agents; the skill reads the active model selection.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
