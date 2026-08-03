from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "codex-custom-subagent" / "scripts"))

import platform_lock  # noqa: E402


class PlatformLockBackendTests(unittest.TestCase):
    def test_posix_backend_is_selected_on_posix(self) -> None:
        with patch.object(platform_lock.os, "name", "posix"):
            self.assertEqual("posix", platform_lock.lock_backend())

    def test_windows_backend_is_selected_on_nt(self) -> None:
        with patch.object(platform_lock.os, "name", "nt"):
            self.assertEqual("windows", platform_lock.lock_backend())

    def test_handle_dispatch_calls_posix_lock(self) -> None:
        with patch.object(platform_lock, "lock_backend", return_value="posix"), patch.object(
            platform_lock, "_posix_lock"
        ) as posix_lock, patch.object(platform_lock, "_windows_lock") as windows_lock:
            platform_lock._lock_handle(7, exclusive=True)
        posix_lock.assert_called_once_with(7, True)
        windows_lock.assert_not_called()

    def test_handle_dispatch_calls_windows_lock(self) -> None:
        with patch.object(platform_lock, "lock_backend", return_value="windows"), patch.object(
            platform_lock, "_posix_lock"
        ) as posix_lock, patch.object(platform_lock, "_windows_lock") as windows_lock:
            platform_lock._lock_handle(9, exclusive=True)
        windows_lock.assert_called_once_with(9, True)
        posix_lock.assert_not_called()

    def test_platform_lock_creates_lock_file_and_releases_on_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / ".lock"
            with patch.object(platform_lock, "lock_backend", return_value="posix"), patch.object(
                platform_lock, "_posix_lock"
            ):
                with platform_lock.platform_file_lock(lock_path):
                    self.assertTrue(lock_path.is_file())
            self.assertTrue(lock_path.is_file())

    def test_posix_lock_release_calls_flock_unlock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / ".lock"
            calls = []
            with patch.object(platform_lock, "lock_backend", return_value="posix"), patch.object(
                platform_lock,
                "_posix_lock",
                side_effect=lambda handle, exclusive: calls.append((handle, exclusive)),
            ):
                with platform_lock.platform_file_lock(lock_path):
                    self.assertEqual(1, len(calls))
                    self.assertTrue(calls[0][1])
            self.assertEqual(2, len(calls))
            self.assertFalse(calls[1][1])

    def test_windows_lock_backend_uses_same_context_manager_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / ".lock"
            calls = []
            with patch.object(platform_lock, "lock_backend", return_value="windows"), patch.object(
                platform_lock, "_windows_lock", side_effect=lambda handle, exclusive: calls.append(exclusive)
            ):
                with platform_lock.platform_file_lock(lock_path):
                    self.assertEqual([True], calls)
            self.assertEqual([True, False], calls)


if __name__ == "__main__":
    unittest.main()
