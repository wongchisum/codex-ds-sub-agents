#!/usr/bin/env python3
"""Check a Codex DeepSeek subagent installation without exposing credentials."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from install import resolve_codex_home, toml_header_present


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KEYCHAIN_TIMEOUT = 5.0
CODEX_TIMEOUT = 30.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", type=Path, default=resolve_codex_home())
    parser.add_argument("--skip-keychain", action="store_true")
    return parser.parse_args()


def report(ok: bool, label: str, detail: str) -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {label}: {detail}")
    return ok


def check_keychain(security: Path, timeout: float = KEYCHAIN_TIMEOUT) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [str(security), "find-generic-password", "-a", "codex", "-s", "deepseek-api-key"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:.0f}s (unlock prompt may be blocking)"
    if result.returncode == 0:
        return True, "deepseek-api-key exists"
    return False, "deepseek-api-key not found"


def _minimal_install(project_root: Path, home: Path) -> None:
    """Install only the DeepSeek files into `home` for an isolated strict-config check."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_bytes((project_root / "config" / "deepseek-provider.toml").read_bytes())
    (home / "agents").mkdir(parents=True, exist_ok=True)
    template = (project_root / "agents" / "deepseek-worker.toml.template").read_text(encoding="utf-8")
    (home / "agents" / "deepseek-worker.toml").write_text(template.replace("__CODEX_HOME__", str(home)), encoding="utf-8")
    (home / "models").mkdir(parents=True, exist_ok=True)
    shutil.copy2(project_root / "models" / "deepseek-v4-flash.json", home / "models" / "deepseek-v4-flash.json")
    shutil.copytree(project_root / "skills" / "deepseek-delegation", home / "skills" / "deepseek-delegation")


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
    checks: list[bool] = []

    agent = codex_home / "agents" / "deepseek-worker.toml"
    model = codex_home / "models" / "deepseek-v4-flash.json"
    skill = codex_home / "skills" / "deepseek-delegation" / "SKILL.md"
    claim = codex_home / "skills" / "deepseek-delegation" / "scripts" / "claim_task.py"
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
