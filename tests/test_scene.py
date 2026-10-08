"""The centrepiece's motion. Its checks are written in JavaScript, like the code they check."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

CHECKS = Path(__file__).parent / "scene_checks.js"


def test_scene_motion():
    gjs = shutil.which("gjs")
    if gjs is None:
        pytest.skip("needs gjs (the JavaScript engine that ships with GNOME) to run the checks")
    result = subprocess.run([gjs, "-m", str(CHECKS)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert " passed, 0 failed" in result.stdout
