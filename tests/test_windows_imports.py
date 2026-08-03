from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WindowsImportSimulationTests(unittest.TestCase):
    def test_common_paths_import_under_simulated_win32(self) -> None:
        """No unconditional fcntl/plist/launchctl dependency in common paths."""
        script = textwrap.dedent(
            """
            import importlib.util
            import plistlib
            import shutil
            import ssl
            import subprocess
            import sys
            import tempfile
            import urllib.error
            import urllib.request
            from pathlib import Path

            # Python 3.13 stdlib modules select private Windows dependencies from
            # sys.platform at import time. Load them before simulating win32 so
            # this test isolates project imports instead of requiring _winapi on macOS.
            if not hasattr(ssl, "enum_certificates"):
                ssl.enum_certificates = lambda _store: ()
            sys.platform = "win32"

            root = Path(sys.argv[1])
            sys.path.insert(0, str(root / "scripts"))
            sys.path.insert(0, str(root / "skills" / "codex-custom-subagents" / "scripts"))

            modules = (
                "scripts/platform_runtime.py",
                "scripts/adapter_service.py",
                "skills/codex-custom-subagents/scripts/platform_lock.py",
                "skills/codex-custom-subagents/scripts/claim_task.py",
                "skills/codex-custom-subagents/scripts/delegation_runtime.py",
            )
            for relative in modules:
                name = Path(relative).stem
                spec = importlib.util.spec_from_file_location(name, root / relative)
                assert spec and spec.loader
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
            print("WIN32_IMPORTS_OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(PROJECT_ROOT)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("WIN32_IMPORTS_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
