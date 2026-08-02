#!/usr/bin/env python3
"""Read and manage subagent credentials without putting secret values in arguments."""

from __future__ import annotations

import argparse
import ctypes
import getpass
import os
import platform
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence


CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
TARGET_PREFIX = "codex-custom-subagents"


class CredentialError(RuntimeError):
    pass


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


PCREDENTIALW = ctypes.POINTER(CREDENTIALW)


@dataclass(frozen=True)
class CredentialIdentity:
    service: str
    account: str

    @property
    def windows_target(self) -> str:
        return f"{TARGET_PREFIX}:{self.service}:{self.account}"


def validate_identity(service: str, account: str) -> CredentialIdentity:
    if not service.strip() or not account.strip():
        raise CredentialError("service and account must be non-empty")
    if any(character in service + account for character in ("\x00", "\r", "\n")):
        raise CredentialError("service and account must not contain control characters")
    return CredentialIdentity(service.strip(), account.strip())


def _advapi32() -> object:
    if os.name != "nt":
        raise CredentialError("Windows Credential Manager is only available on Windows")
    try:
        return ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    except (AttributeError, OSError) as error:
        raise CredentialError(f"cannot load Windows Credential Manager: {error}") from error


def _configure_windows_api(api: object) -> None:
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCREDENTIALW)]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None


def windows_read(identity: CredentialIdentity, api: Optional[object] = None) -> str:
    manager = api or _advapi32()
    _configure_windows_api(manager)
    pointer = PCREDENTIALW()
    if not manager.CredReadW(identity.windows_target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            raise CredentialError(f"credential not found: {identity.service}")
        raise CredentialError(f"CredReadW failed with Windows error {error}")
    try:
        credential = pointer.contents
        raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CredentialError("stored credential is not valid UTF-8") from error
    finally:
        manager.CredFree(pointer)


def windows_write(identity: CredentialIdentity, secret: str, api: Optional[object] = None) -> None:
    if not secret:
        raise CredentialError("credential value must not be empty")
    manager = api or _advapi32()
    _configure_windows_api(manager)
    encoded = secret.encode("utf-8")
    buffer = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
    credential = CREDENTIALW(
        Type=CRED_TYPE_GENERIC,
        TargetName=identity.windows_target,
        CredentialBlobSize=len(encoded),
        CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        Persist=CRED_PERSIST_LOCAL_MACHINE,
        UserName=identity.account,
    )
    if not manager.CredWriteW(ctypes.byref(credential), 0):
        raise CredentialError(
            f"CredWriteW failed with Windows error {ctypes.get_last_error()}"
        )


def windows_delete(identity: CredentialIdentity, api: Optional[object] = None) -> bool:
    manager = api or _advapi32()
    _configure_windows_api(manager)
    if manager.CredDeleteW(identity.windows_target, CRED_TYPE_GENERIC, 0):
        return True
    error = ctypes.get_last_error()
    if error == ERROR_NOT_FOUND:
        return False
    raise CredentialError(f"CredDeleteW failed with Windows error {error}")


def macos_read(
    identity: CredentialIdentity,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    security = Path("/usr/bin/security")
    if not security.is_file():
        raise CredentialError("macOS security command not found")
    result = runner(
        [
            str(security),
            "find-generic-password",
            "-a",
            identity.account,
            "-s",
            identity.service,
            "-w",
        ],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        raise CredentialError(f"credential not found: {identity.service}")
    return result.stdout.rstrip("\r\n")


def read_credential(
    identity: CredentialIdentity,
    *,
    system: Optional[str] = None,
) -> str:
    current = system or platform.system()
    if current == "Windows":
        return windows_read(identity)
    if current == "Darwin":
        return macos_read(identity)
    raise CredentialError(
        "no native credential store backend for this platform; use an environment reference"
    )


def credential_exists(identity: CredentialIdentity, *, system: Optional[str] = None) -> bool:
    try:
        read_credential(identity, system=system)
    except CredentialError:
        return False
    return True


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("get", "set", "exists", "delete"))
    parser.add_argument("--service", required=True)
    parser.add_argument("--account", default="codex")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        identity = validate_identity(args.service, args.account)
        current = platform.system()
        if args.action == "get":
            print(read_credential(identity, system=current))
            return 0
        if args.action == "exists":
            return 0 if credential_exists(identity, system=current) else 1
        if current != "Windows":
            raise CredentialError(
                "set/delete are currently implemented for Windows Credential Manager only; "
                "on macOS use the interactive security command printed by configure.py"
            )
        if args.action == "set":
            first = getpass.getpass("Credential: ")
            second = getpass.getpass("Confirm credential: ")
            if first != second:
                raise CredentialError("credential confirmation does not match")
            windows_write(identity, first)
            print(f"stored credential reference: {identity.service}")
            return 0
        removed = windows_delete(identity)
        print(f"{'deleted' if removed else 'not found'}: {identity.service}")
        return 0
    except (CredentialError, OSError, subprocess.TimeoutExpired) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
