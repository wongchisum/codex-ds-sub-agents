#!/usr/bin/env python3
"""Install and control persistent local adapter services per platform.

macOS uses LaunchAgents via launchctl; Windows uses per-user Task Scheduler
tasks via schtasks (no administrator rights). The Windows backend is selected
at runtime; this module imports on both platforms without fcntl, plist, or
launchctl dependencies in common paths."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from anthropic_responses_adapter import service_fingerprint
from install import resolve_codex_home
from model_manifest import ModelManifest, ProviderSpec, catalog_filename, load_manifest
from model_selection import ValidationError
import platform_runtime
from platform_runtime import (
    adapter_paths,
    python_command,
    quote_windows_command,
    service_command,
)


LABEL_PREFIX = "com.openai.codex.subagent-adapter"
HEALTH_TIMEOUT_SECONDS = 2.0
STARTUP_TIMEOUT_SECONDS = 15.0
STARTUP_POLL_INTERVAL_SECONDS = 0.2
MAX_HEALTH_RESPONSE_BYTES = 4096


@dataclass(frozen=True)
class ServiceSpec:
    provider_id: str
    label: str
    plist_path: Path
    health_url: str
    fingerprint: str
    arguments: tuple[str, ...]
    stdout_path: Path
    stderr_path: Path
    task_name: str | None = None

    @property
    def definition_path(self) -> Path:
        """The managed service definition file (plist on macOS, task XML on Windows)."""
        return self.plist_path


class ServiceError(RuntimeError):
    pass


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def provider_catalog(manifest: ModelManifest, provider: ProviderSpec, codex_home: Path) -> Path:
    models = [model for model in manifest.models.values() if model.provider_id == provider.id]
    if len(models) != 1:
        raise ServiceError(
            f"provider {provider.id} must currently bind exactly one model catalog; found {len(models)}"
        )
    return codex_home / "models" / catalog_filename(models[0])


def service_specs(
    manifest: ModelManifest,
    codex_home: Path,
    launch_agents_dir: Path,
    python: str | None = None,
) -> tuple[ServiceSpec, ...]:
    specs = []
    paths = adapter_paths(codex_home)
    log_dir = paths.logs_dir
    interpreter = python or python_command()
    for provider in manifest.providers.values():
        adapter = provider.adapter
        if adapter is None:
            continue
        label = f"{LABEL_PREFIX}.{provider.id}"
        stdout_path = log_dir / f"{provider.id}.stdout.log"
        stderr_path = log_dir / f"{provider.id}.stderr.log"
        arguments = service_command(
            interpreter,
            paths.scripts_dir / "anthropic_responses_adapter.py",
            listen_host=adapter.listen_host,
            port=adapter.listen_port,
            service_id=provider.id,
            max_output_tokens=adapter.max_output_tokens,
            upstream_base_url=provider.base_url,
            model_catalog=provider_catalog(manifest, provider, codex_home),
            audit_log=log_dir / f"{provider.id}.audit.jsonl",
        )
        if platform_runtime.is_windows():
            definition_path = paths.definitions_dir / f"{label}.xml"
            arguments = (
                interpreter,
                str(paths.scripts_dir / "service_runner.py"),
                "--stdout-log",
                str(stdout_path),
                "--stderr-log",
                str(stderr_path),
                "--",
                *arguments,
            )
        else:
            definition_path = launch_agents_dir / f"{label}.plist"
        specs.append(ServiceSpec(
            provider_id=provider.id,
            label=label,
            plist_path=definition_path,
            health_url=f"http://{adapter.listen_host}:{adapter.listen_port}/health",
            fingerprint=service_fingerprint(provider.id, provider.base_url),
            arguments=arguments,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            task_name=label if platform_runtime.is_windows() else None,
        ))
    return tuple(specs)


def filter_service_specs(
    specs: Iterable[ServiceSpec],
    provider_ids: Optional[Iterable[str]],
) -> tuple[ServiceSpec, ...]:
    available = tuple(specs)
    if provider_ids is None:
        return available
    requested = set(provider_ids)
    known = {spec.provider_id for spec in available}
    unknown = requested - known
    if unknown:
        raise ServiceError(
            "manifest has no adapter provider: " + ", ".join(sorted(unknown))
        )
    return tuple(spec for spec in available if spec.provider_id in requested)


def render_plist(spec: ServiceSpec) -> bytes:
    payload = {
        "Label": spec.label,
        "ProgramArguments": list(spec.arguments),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "ProcessType": "Background",
        "StandardOutPath": str(spec.stdout_path),
        "StandardErrorPath": str(spec.stderr_path),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def launchctl(arguments: Iterable[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *arguments], text=True, capture_output=True, check=False
    )


def schtasks(arguments: Iterable[str]) -> subprocess.CompletedProcess[str]:
    """Run the Windows Task Scheduler CLI (only called on Windows hosts)."""
    return subprocess.run(
        ["schtasks", *arguments], text=True, capture_output=True, check=False
    )


def windows_task_name(spec: ServiceSpec) -> str:
    return spec.task_name or spec.label


def windows_query(spec: ServiceSpec) -> subprocess.CompletedProcess[str]:
    return schtasks(("/Query", "/TN", windows_task_name(spec), "/FO", "LIST"))


def windows_create(spec: ServiceSpec) -> subprocess.CompletedProcess[str]:
    return schtasks(
        ("/Create", "/TN", windows_task_name(spec), "/XML", str(spec.definition_path), "/F")
    )


def windows_run(spec: ServiceSpec) -> subprocess.CompletedProcess[str]:
    return schtasks(("/Run", "/TN", windows_task_name(spec)))


def windows_end(spec: ServiceSpec) -> subprocess.CompletedProcess[str]:
    return schtasks(("/End", "/TN", windows_task_name(spec)))


def windows_delete(spec: ServiceSpec) -> subprocess.CompletedProcess[str]:
    return schtasks(("/Delete", "/TN", windows_task_name(spec), "/F"))


def windows_task_xml_text(spec: ServiceSpec) -> str:
    """Render the Task Scheduler 2.0 definition for one adapter service.

    The task runs at logon with the interactive user token at least privilege,
    matching the macOS RunAtLoad/KeepAlive semantics without administrator
    rights. Console output is not redirected because Task Scheduler does not
    support StandardOutPath/StandardErrorPath equivalents.
    """
    from xml.sax.saxutils import escape

    python = spec.arguments[0]
    script = spec.arguments[1]
    arguments = quote_windows_command(spec.arguments[1:])
    working_directory = str(Path(script).parent)
    label = escape(windows_task_name(spec))
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        '  <RegistrationInfo>\n'
        f'    <Description>codex subagent adapter service</Description>\n'
        f'    <URI>\\{label}</URI>\n'
        '  </RegistrationInfo>\n'
        '  <Triggers>\n'
        '    <LogonTrigger>\n'
        '      <Enabled>true</Enabled>\n'
        '    </LogonTrigger>\n'
        '  </Triggers>\n'
        '  <Principals>\n'
        '    <Principal id="Author">\n'
        '      <LogonType>InteractiveToken</LogonType>\n'
        '      <RunLevel>LeastPrivilege</RunLevel>\n'
        '    </Principal>\n'
        '  </Principals>\n'
        '  <Settings>\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
        '    <AllowHardTerminate>true</AllowHardTerminate>\n'
        '    <StartWhenAvailable>true</StartWhenAvailable>\n'
        '    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n'
        '    <RestartOnFailure>\n'
        '      <Interval>PT1M</Interval>\n'
        '      <Count>3</Count>\n'
        '    </RestartOnFailure>\n'
        '  </Settings>\n'
        '  <Actions>\n'
        '    <Exec>\n'
        f'      <Command>{escape(python)}</Command>\n'
        f'      <Arguments>{escape(arguments)}</Arguments>\n'
        f'      <WorkingDirectory>{escape(working_directory)}</WorkingDirectory>\n'
        '    </Exec>\n'
        '  </Actions>\n'
        '</Task>\n'
    )


def windows_task_xml(spec: ServiceSpec) -> bytes:
    """Render the Windows task definition encoded as schtasks-compatible UTF-16."""
    return windows_task_xml_text(spec).encode("utf-16")


def render_definition(spec: ServiceSpec) -> bytes:
    """Render the platform-specific managed service definition file."""
    return windows_task_xml(spec) if platform_runtime.is_windows() else render_plist(spec)


def check_health_url(
    health_url: str,
    service_id: str,
    fingerprint: str,
    timeout: float = HEALTH_TIMEOUT_SECONDS,
    opener: Optional[Callable[..., object]] = None,
) -> tuple[bool, str]:
    request = urllib.request.Request(
        health_url,
        method="GET",
        headers={"User-Agent": "codex-subagent-adapter-service/1"},
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            raw_body = response.read(MAX_HEALTH_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        return False, f"{health_url} returned HTTP {error.code}"
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        return False, f"{health_url} unavailable: {error}"
    if status != 200:
        return False, f"{health_url} returned HTTP {status}"
    if len(raw_body) > MAX_HEALTH_RESPONSE_BYTES:
        return False, f"{health_url} response exceeds {MAX_HEALTH_RESPONSE_BYTES} bytes"
    try:
        body = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return False, f"{health_url} returned invalid JSON: {error}"
    if not isinstance(body, dict):
        return False, f"{health_url} response must be an object"
    if body.get("status") != "ok" or body.get("adapter") != "anthropic_messages":
        return False, f"{health_url} returned an unexpected adapter health payload"
    if body.get("service_id") != service_id or body.get("fingerprint") != fingerprint:
        return False, (
            f"{health_url} identity mismatch for service {service_id}; "
            f"expected fingerprint {fingerprint}"
        )
    return True, f"{health_url} is healthy"


def health(spec: ServiceSpec, timeout: float = HEALTH_TIMEOUT_SECONDS) -> tuple[bool, str]:
    return check_health_url(
        spec.health_url,
        spec.provider_id,
        spec.fingerprint,
        timeout,
    )


def wait_for_health(
    spec: ServiceSpec,
    timeout: float = STARTUP_TIMEOUT_SECONDS,
    interval: float = STARTUP_POLL_INTERVAL_SECONDS,
    probe: Optional[Callable[[ServiceSpec, float], tuple[bool, str]]] = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    if timeout <= 0 or interval <= 0:
        raise ValueError("health wait timeout and interval must be positive")
    health_probe = probe or health
    deadline = clock() + timeout
    last_detail = "health endpoint was not checked"
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            break
        ok, last_detail = health_probe(spec, min(HEALTH_TIMEOUT_SECONDS, remaining))
        if ok:
            return
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleeper(min(interval, remaining))
    raise ServiceError(
        f"service {spec.label} did not become healthy within {timeout:.1f}s: {last_detail}"
    )


def _existing_plist_is_managed(spec: ServiceSpec, expected: bytes) -> bool:
    if spec.plist_path.is_symlink():
        raise ServiceError(f"refusing to replace symlinked service file: {spec.plist_path}")
    if not spec.plist_path.exists():
        return False
    if not spec.plist_path.is_file():
        raise ServiceError(f"refusing to replace non-regular service file: {spec.plist_path}")
    if spec.plist_path.read_bytes() != expected:
        raise ServiceError(f"refusing to replace modified service definition: {spec.plist_path}")
    return True


def _remove_plist_created_by_start(spec: ServiceSpec, expected: bytes) -> None:
    if (
        spec.plist_path.is_symlink()
        or not spec.plist_path.is_file()
        or spec.plist_path.read_bytes() != expected
    ):
        raise ServiceError(
            f"refusing to remove changed service definition during rollback: {spec.plist_path}"
        )
    spec.plist_path.unlink()


def _rollback_started_job(
    spec: ServiceSpec,
    service_target: str,
    created_plist: bool,
    expected: bytes,
    cause: BaseException,
) -> None:
    try:
        stopped = launchctl(("bootout", service_target))
    except OSError as error:
        raise ServiceError(
            f"{cause}; rollback could not stop {spec.label}: {error}"
        ) from cause
    if stopped.returncode != 0:
        detail = stopped.stderr.strip() or f"cannot stop {spec.label}"
        raise ServiceError(f"{cause}; rollback failed: {detail}") from cause
    if created_plist:
        try:
            _remove_plist_created_by_start(spec, expected)
        except (OSError, ServiceError) as error:
            raise ServiceError(
                f"{cause}; job stopped but rollback could not remove plist: {error}"
            ) from cause


def _restore_previous_job(
    spec: ServiceSpec,
    domain: str,
    cause: BaseException,
) -> None:
    try:
        restored = launchctl(("bootstrap", domain, str(spec.plist_path)))
    except OSError as error:
        raise ServiceError(
            f"{cause}; restoring previous job failed: {error}"
        ) from cause
    if restored.returncode != 0:
        detail = restored.stderr.strip() or f"cannot restore {spec.label}"
        raise ServiceError(
            f"{cause}; restoring previous job failed: {detail}"
        ) from cause


def _restore_windows_task(spec: ServiceSpec, cause: BaseException) -> None:
    try:
        restored = windows_create(spec)
    except OSError as error:
        raise ServiceError(
            f"{cause}; restoring previous task failed: {error}"
        ) from error
    if restored.returncode != 0:
        detail = restored.stderr.strip() or f"cannot restore {spec.label}"
        raise ServiceError(
            f"{cause}; restoring previous task failed: {detail}"
        ) from cause


def _rollback_windows_task(
    spec: ServiceSpec,
    created_definition: bool,
    expected: bytes,
    cause: BaseException,
) -> None:
    try:
        ended = windows_end(spec)
    except OSError as error:
        raise ServiceError(
            f"{cause}; rollback could not stop {spec.label}: {error}"
        ) from cause
    if ended.returncode != 0:
        detail = ended.stderr.strip() or f"cannot stop {spec.label}"
        raise ServiceError(f"{cause}; rollback failed: {detail}") from cause
    removed = windows_delete(spec)
    if removed.returncode != 0:
        detail = removed.stderr.strip() or f"cannot delete {spec.label}"
        raise ServiceError(
            f"{cause}; rollback could not delete task: {detail}"
        ) from cause
    if created_definition:
        try:
            _remove_plist_created_by_start(spec, expected)
        except (OSError, ServiceError) as error:
            raise ServiceError(
                f"{cause}; task removed but rollback could not remove definition: {error}"
            ) from cause


def _install_windows(spec: ServiceSpec, uid: int | None) -> None:
    spec.stdout_path.parent.mkdir(parents=True, exist_ok=True)
    expected = windows_task_xml(spec)
    managed_definition_exists = _existing_plist_is_managed(spec, expected)
    existing = windows_query(spec)
    was_loaded = existing.returncode == 0
    if was_loaded:
        if not managed_definition_exists:
            raise ServiceError(
                f"service {spec.label} is already registered without a verifiable "
                "managed definition; refusing to replace it"
            )
        ended = windows_end(spec)
        if ended.returncode != 0:
            raise ServiceError(ended.stderr.strip() or f"cannot stop {spec.label}")
    created_definition = not managed_definition_exists
    if created_definition:
        atomic_write(spec.plist_path, expected)
    try:
        started = windows_create(spec)
    except BaseException as error:
        if created_definition:
            _remove_plist_created_by_start(spec, expected)
        elif was_loaded:
            _restore_windows_task(spec, error)
        raise
    if started.returncode != 0:
        error = ServiceError(started.stderr.strip() or f"cannot start {spec.label}")
        if created_definition:
            _remove_plist_created_by_start(spec, expected)
        elif was_loaded:
            _restore_windows_task(spec, error)
        raise error
    try:
        kicked = windows_run(spec)
        if kicked.returncode != 0:
            raise ServiceError(kicked.stderr.strip() or f"cannot run {spec.label}")
        wait_for_health(spec)
    except BaseException as error:
        _rollback_windows_task(spec, created_definition, expected, error)
        if was_loaded:
            _restore_windows_task(spec, error)
        raise


def _stop_windows(spec: ServiceSpec, uid: int | None) -> None:
    if not spec.plist_path.exists():
        existing = windows_query(spec)
        if existing.returncode == 0:
            raise ServiceError(
                f"service {spec.label} is registered but {spec.plist_path} is missing; "
                "refusing to stop an unverifiable task"
            )
        return
    if spec.plist_path.is_symlink() or not spec.plist_path.is_file():
        raise ServiceError(f"refusing to remove non-regular service file: {spec.plist_path}")
    if spec.plist_path.read_bytes() != windows_task_xml(spec):
        raise ServiceError(f"refusing to stop modified service definition: {spec.plist_path}")
    existing = windows_query(spec)
    if existing.returncode == 0:
        ended = windows_end(spec)
        if ended.returncode != 0:
            raise ServiceError(ended.stderr.strip() or f"cannot stop {spec.label}")
        removed = windows_delete(spec)
        if removed.returncode != 0:
            raise ServiceError(removed.stderr.strip() or f"cannot delete {spec.label}")
    spec.plist_path.unlink(missing_ok=True)


def install_and_start(spec: ServiceSpec, uid: int | None) -> None:
    if platform_runtime.is_windows():
        _install_windows(spec, uid)
    else:
        _install_macos(spec, uid)


def stop_and_remove(spec: ServiceSpec, uid: int | None) -> None:
    if platform_runtime.is_windows():
        _stop_windows(spec, uid)
    else:
        _stop_macos(spec, uid)


def _install_macos(spec: ServiceSpec, uid: int) -> None:
    spec.stdout_path.parent.mkdir(parents=True, exist_ok=True)
    expected = render_plist(spec)
    managed_plist_exists = _existing_plist_is_managed(spec, expected)
    domain = f"gui/{uid}"
    service_target = f"{domain}/{spec.label}"
    existing = launchctl(("print", service_target))
    was_loaded = existing.returncode == 0
    if was_loaded:
        if not managed_plist_exists:
            raise ServiceError(
                f"service {spec.label} is already loaded without a verifiable managed plist; "
                "refusing to replace it"
            )
        stopped = launchctl(("bootout", service_target))
        if stopped.returncode != 0:
            raise ServiceError(stopped.stderr.strip() or f"cannot stop {spec.label}")
    created_plist = not managed_plist_exists
    if created_plist:
        atomic_write(spec.plist_path, expected)
    try:
        started = launchctl(("bootstrap", domain, str(spec.plist_path)))
    except BaseException as error:
        if created_plist:
            _remove_plist_created_by_start(spec, expected)
        elif was_loaded:
            _restore_previous_job(spec, domain, error)
        raise
    if started.returncode != 0:
        error = ServiceError(started.stderr.strip() or f"cannot start {spec.label}")
        if created_plist:
            _remove_plist_created_by_start(spec, expected)
        elif was_loaded:
            _restore_previous_job(spec, domain, error)
        raise error
    try:
        kicked = launchctl(("kickstart", "-k", service_target))
        if kicked.returncode != 0:
            raise ServiceError(kicked.stderr.strip() or f"cannot kickstart {spec.label}")
        wait_for_health(spec)
    except BaseException as error:
        _rollback_started_job(
            spec,
            service_target,
            created_plist,
            expected,
            error,
        )
        if was_loaded:
            _restore_previous_job(spec, domain, error)
        raise


def _stop_macos(spec: ServiceSpec, uid: int) -> None:
    domain = f"gui/{uid}"
    service_target = f"{domain}/{spec.label}"
    if not spec.plist_path.exists():
        existing = launchctl(("print", service_target))
        if existing.returncode == 0:
            raise ServiceError(
                f"service {spec.label} is loaded but {spec.plist_path} is missing; "
                "refusing to stop an unverifiable job"
            )
        return
    if spec.plist_path.is_symlink() or not spec.plist_path.is_file():
        raise ServiceError(f"refusing to remove non-regular service file: {spec.plist_path}")
    if spec.plist_path.read_bytes() != render_plist(spec):
        raise ServiceError(f"refusing to stop modified service definition: {spec.plist_path}")
    existing = launchctl(("print", service_target))
    if existing.returncode == 0:
        result = launchctl(("bootout", service_target))
        if result.returncode != 0:
            raise ServiceError(result.stderr.strip() or f"cannot stop {spec.label}")
    spec.plist_path.unlink(missing_ok=True)


def _current_uid() -> int | None:
    """Return the numeric user id on POSIX; None on Windows (not required there)."""
    return os.getuid() if hasattr(os, "getuid") else None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status", "render"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, default=resolve_codex_home())
    parser.add_argument("--launch-agents-dir", type=Path, default=Path.home() / "Library" / "LaunchAgents")
    parser.add_argument(
        "--provider",
        action="append",
        dest="providers",
        default=None,
        help="limit the action to this manifest adapter provider; may be repeated",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = load_manifest(args.manifest.expanduser().resolve())
        codex_home = args.codex_home.expanduser().resolve()
        specs = filter_service_specs(
            service_specs(
                manifest,
                codex_home,
                args.launch_agents_dir.expanduser().resolve(),
            ),
            args.providers,
        )
        if not specs:
            raise ServiceError("no adapter providers selected")
        if args.action == "render":
            for spec in specs:
                atomic_write(spec.plist_path, render_definition(spec))
                print(f"rendered: {spec.plist_path}")
            return 0
        if args.action == "start":
            for spec in specs:
                install_and_start(spec, _current_uid())
                print(f"started: {spec.label}")
            return 0
        if args.action == "stop":
            for spec in specs:
                stop_and_remove(spec, _current_uid())
                print(f"stopped: {spec.label}")
            return 0
        all_healthy = True
        for spec in specs:
            ok, detail = health(spec)
            all_healthy = all_healthy and ok
            print(f"{'PASS' if ok else 'FAIL'}  {spec.provider_id}: {spec.health_url}: {detail}")
        return 0 if all_healthy else 1
    except (OSError, ServiceError, ValidationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
