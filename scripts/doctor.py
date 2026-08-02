#!/usr/bin/env python3
"""Check a Codex Custom Subagents installation without exposing credentials."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

from adapter_service import check_health_url, service_fingerprint
from credential_store import CredentialError, credential_exists, validate_identity
from install import resolve_codex_home, toml_header_present
from platform_runtime import python_command_toml, toml_path_escape
from model_manifest import (
    catalog_filename,
    load_manifest,
    render_agent,
    render_model_catalog,
    render_provider,
    unsupported_protocols,
)
from model_selection import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KEYCHAIN_TIMEOUT = 5.0
CODEX_TIMEOUT = 30.0
ADAPTER_HEALTH_TIMEOUT = 2.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=resolve_codex_home())
    parser.add_argument("--skip-keychain", action="store_true")
    parser.add_argument("--skip-adapter-health", action="store_true")
    parser.add_argument("--manifest", type=Path, default=None)
    return parser.parse_args()


def report(ok: bool, label: str, detail: str) -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {label}: {detail}")
    return ok


def check_keychain(
    security: Path,
    timeout: float = KEYCHAIN_TIMEOUT,
    account: str = "codex",
    service: str = "deepseek-api-key",
) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [str(security), "find-generic-password", "-a", account, "-s", service],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:.0f}s (unlock prompt may be blocking)"
    if result.returncode == 0:
        return True, f"{service} exists"
    return False, f"{service} not found"


def check_adapter_health(
    base_url: str,
    service_id: str,
    fingerprint: str,
    timeout: float = ADAPTER_HEALTH_TIMEOUT,
    opener: Optional[Callable[..., object]] = None,
) -> tuple[bool, str]:
    health_url = base_url.rstrip("/") + "/health"
    return check_health_url(
        health_url,
        service_id,
        fingerprint,
        timeout,
        opener,
    )


def check_custom_install(args: argparse.Namespace, codex_home: Path) -> int:
    checks: list[bool] = []
    try:
        manifest = load_manifest(args.manifest.expanduser().resolve())
    except ValidationError as error:
        report(False, "manifest", str(error))
        return 1

    config = codex_home / "config.toml"
    selection = codex_home / "models" / "subagent-selection.json"
    checks.append(report(config.is_file(), "config", str(config)))
    expected_selection = json.dumps(manifest.normalized_selection(), indent=2, sort_keys=True) + "\n"
    selection_matches = selection.is_file() and selection.read_text(encoding="utf-8") == expected_selection
    checks.append(report(selection_matches, "selection", str(selection)))
    content = config.read_text(encoding="utf-8") if config.is_file() else ""
    credential_command = (
        sys.executable,
        str(codex_home / "helpers" / "credential_store.py"),
    )
    for provider in manifest.providers.values():
        provider_matches = render_provider(provider, credential_command).strip() in content
        checks.append(
            report(
                toml_header_present(content, f"[model_providers.{provider.id}]") and provider_matches,
                f"provider {provider.id}",
                provider.effective_base_url,
            )
        )
        if provider.auth.kind == "keychain":
            helper = codex_home / "helpers" / "credential_store.py"
            source_helper = PROJECT_ROOT / "scripts" / "credential_store.py"
            checks.append(
                report(
                    helper.is_file() and helper.read_bytes() == source_helper.read_bytes(),
                    f"credential helper {provider.id}",
                    str(helper),
                )
            )
            checks.append(
                report(
                    toml_header_present(content, f"[model_providers.{provider.id}.auth]"),
                    f"provider auth {provider.id}",
                    provider.auth.name,
                )
            )
        if provider.adapter is not None:
            for filename in ("anthropic_adapter_protocol.py", "anthropic_responses_adapter.py", "service_runner.py"):
                source = PROJECT_ROOT / "scripts" / filename
                installed = codex_home / "adapters" / filename
                checks.append(
                    report(
                        installed.is_file() and installed.read_bytes() == source.read_bytes(),
                        f"adapter {provider.id}",
                        str(installed),
                    )
                )
            if not args.skip_adapter_health:
                fingerprint = service_fingerprint(provider.id, provider.base_url)
                healthy, detail = check_adapter_health(
                    provider.adapter.base_url,
                    provider.id,
                    fingerprint,
                )
                checks.append(report(healthy, f"adapter health {provider.id}", detail))
    template = (PROJECT_ROOT / "agents" / "model-worker.toml.template").read_text(encoding="utf-8")
    for model in manifest.models.values():
        agent = codex_home / "agents" / f"{model.agent}.toml"
        expected_agent = render_agent(template, codex_home, model)
        agent_matches = agent.is_file() and agent.read_text(encoding="utf-8") == expected_agent
        checks.append(report(agent_matches, f"agent {model.id}", str(agent)))
        installed = codex_home / "models" / catalog_filename(model)
        catalog_matches = installed.is_file() and installed.read_bytes() == render_model_catalog(model)
        checks.append(report(catalog_matches, f"model catalog {model.id}", str(installed)))

    unsupported = unsupported_protocols(manifest)
    checks.append(
        report(
            not unsupported,
            "wire API",
            "responses" if not unsupported else "unsupported: " + ", ".join(unsupported),
        )
    )
    if not args.skip_keychain:
        seen = set()
        for provider in manifest.providers.values():
            if provider.auth.kind != "keychain":
                continue
            identity = (provider.auth.account, provider.auth.name)
            if identity in seen:
                continue
            seen.add(identity)
            try:
                identity = validate_identity(provider.auth.name, provider.auth.account)
                ok = credential_exists(identity)
                detail = f"{provider.auth.name} {'exists' if ok else 'not found'}"
            except CredentialError as error:
                ok, detail = False, str(error)
            checks.append(report(ok, "credential store", detail))

    bundled_codex = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    codex = shutil.which("codex") or (str(bundled_codex) if bundled_codex.is_file() else None)
    checks.append(report(codex is not None, "codex", codex or "not found"))
    if codex is not None:
        returncode, detail = _run_codex(codex, codex_home, CODEX_TIMEOUT)
        checks.append(report(returncode == 0, "strict config", detail or "config loads cleanly"))
    return 0 if all(checks) else 1


def _minimal_install(project_root: Path, home: Path) -> None:
    """Install only the DeepSeek files into `home` for an isolated strict-config check."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_bytes((project_root / "config" / "deepseek-provider.toml").read_bytes())
    (home / "agents").mkdir(parents=True, exist_ok=True)
    template = (project_root / "agents" / "deepseek-worker.toml.template").read_text(encoding="utf-8")
    rendered = template.replace("__CODEX_HOME__", toml_path_escape(str(home)))
    (home / "agents" / "deepseek-worker.toml").write_text(
        rendered.replace("__PYTHON_COMMAND__", python_command_toml()),
        encoding="utf-8",
    )
    (home / "models").mkdir(parents=True, exist_ok=True)
    shutil.copy2(project_root / "models" / "deepseek-v4-flash.json", home / "models" / "deepseek-v4-flash.json")
    shutil.copytree(project_root / "skills" / "codex-custom-subagents", home / "skills" / "codex-custom-subagents")


def _run_codex(codex: str, codex_home: Path, timeout: float) -> tuple[int, str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    try:
        result = subprocess.run(
            [codex, "--strict-config", "--version"],
            text=True,
            capture_output=True,
            env=environment,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout:.0f}s"
    return result.returncode, (result.stdout.strip() or result.stderr.strip())


def check_strict_config(
    codex: str, codex_home: Path, project_root: Path, timeout: float = CODEX_TIMEOUT
) -> tuple[bool, str]:
    returncode, detail = _run_codex(codex, codex_home, timeout)
    if returncode == 0:
        return True, detail or "config loads cleanly"
    if returncode == -1:
        return False, f"timed out after {timeout:.0f}s"
    with tempfile.TemporaryDirectory() as directory:
        isolated = Path(directory)
        try:
            _minimal_install(project_root, isolated)
            isolated_rc, isolated_detail = _run_codex(codex, isolated, timeout)
        except OSError as error:
            return False, f"{detail or '(no output)'}; could not run isolated DeepSeek check: {error}"
    if isolated_rc == 0:
        return False, f"user config issue (DeepSeek files pass in isolation): {detail or '(no output)'}"
    if isolated_rc == -1:
        return False, f"DeepSeek installation issue: {detail or '(no output)'}; isolated check timed out"
    return False, f"DeepSeek installation issue: {detail or '(no output)'}; isolated: {isolated_detail or '(no output)'}"


def main() -> int:
    args = parse_args()
    codex_home = args.codex_home.expanduser().resolve()
    if args.manifest is not None:
        return check_custom_install(args, codex_home)
    checks: list[bool] = []

    agent = codex_home / "agents" / "deepseek-worker.toml"
    model = codex_home / "models" / "deepseek-v4-flash.json"
    skill = codex_home / "skills" / "codex-custom-subagents" / "SKILL.md"
    claim = codex_home / "skills" / "codex-custom-subagents" / "scripts" / "claim_task.py"
    config = codex_home / "config.toml"

    for label, path in (("agent", agent), ("model", model), ("skill", skill), ("claim script", claim), ("config", config)):
        checks.append(report(path.is_file(), label, str(path)))

    if model.is_file():
        try:
            slugs = [entry.get("slug") for entry in json.loads(model.read_text(encoding="utf-8")).get("models", [])]
            checks.append(report("deepseek-v4-flash" in slugs, "model catalog", "deepseek-v4-flash"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            checks.append(report(False, "model catalog", str(error)))

    if config.is_file():
        content = config.read_text(encoding="utf-8")
        checks.append(report(toml_header_present(content, "[model_providers.deepseek]"), "provider", "DeepSeek block present"))
        checks.append(report(toml_header_present(content, "[model_providers.deepseek.auth]"), "provider auth", "auth block present"))

    if not args.skip_keychain:
        security = Path("/usr/bin/security")
        if security.is_file():
            ok, detail = check_keychain(security)
            checks.append(report(ok, "keychain", detail))
        else:
            checks.append(report(False, "keychain", "macOS security command not found"))

    bundled_codex = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    codex = shutil.which("codex") or (str(bundled_codex) if bundled_codex.is_file() else None)
    checks.append(report(codex is not None, "codex", codex or "not found"))
    if codex is not None:
        ok, detail = check_strict_config(codex, codex_home, PROJECT_ROOT)
        checks.append(report(ok, "strict config", detail))
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
