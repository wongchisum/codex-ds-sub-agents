from __future__ import annotations

import os
import unittest
from pathlib import Path


def create_symlink_or_skip(
    testcase: unittest.TestCase,
    link: Path,
    target: Path,
    *,
    target_is_directory: bool = False,
) -> None:
    """Create a symlink, or skip when this Windows runner lacks the privilege."""
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            testcase.skipTest("Windows symlink privilege is unavailable")
        raise
