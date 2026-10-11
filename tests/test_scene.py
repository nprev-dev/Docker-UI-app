"""The centrepiece's motion. Its checks are written in JavaScript, like the code they check."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

CHECKS = Path(__file__).parent / "scene_checks.js"


def test_scene_motion():
    # gjs ships with GNOME desktops; node does the job everywhere else.
    gjs, node = shutil.which("gjs"), shutil.which("node")
    if gjs:
        command = [gjs, "-m", str(CHECKS)]
    elif node:
        command = [node, str(CHECKS)]
    else:
        pytest.skip("needs gjs or node to run the JavaScript checks")
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if not gjs and "Cannot use import statement" in result.stderr:
        pytest.skip("this node is too old to load the page's modules")
    assert result.returncode == 0, result.stdout + result.stderr
    assert " passed, 0 failed" in result.stdout
