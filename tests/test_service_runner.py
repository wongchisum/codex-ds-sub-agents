from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import service_runner  # noqa: E402


class ServiceRunnerTests(unittest.TestCase):
    def test_requires_service_command(self) -> None:
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                service_runner.parse_args(
                    ["--stdout-log", "out.log", "--stderr-log", "err.log", "--"]
                )

    def test_appends_child_stdout_and_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stdout = root / "logs" / "stdout.log"
            stderr = root / "logs" / "stderr.log"
            code = "import sys; print('out-line'); print('err-line', file=sys.stderr)"
            result = service_runner.main(
                [
                    "--stdout-log",
                    str(stdout),
                    "--stderr-log",
                    str(stderr),
                    "--",
                    sys.executable,
                    "-c",
                    code,
                ]
            )
            self.assertEqual(0, result)
            self.assertEqual("out-line\n", stdout.read_text(encoding="utf-8"))
            self.assertEqual("err-line\n", stderr.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
