"""Where the dashboard finds its own files, and where it keeps what must survive a restart."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

NAME = "rack-dashboard"
PACKAGE = Path(__file__).resolve().parent
# The page is shipped inside the package, so it is there however the dashboard was installed.
FRONTEND = PACKAGE / "frontend"


def data_dir(platform: str = sys.platform, environ: Mapping[str, str] = os.environ, home: Path | None = None) -> Path:
    """The user's own folder for an application's data, by the custom of each system."""
    home = home or Path.home()
    if platform == "win32":
        base = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    elif platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        # A relative XDG_DATA_HOME is not valid and must be ignored.
        given = environ.get("XDG_DATA_HOME", "")
        base = Path(given) if given.startswith("/") else home / ".local" / "share"
    return base / NAME


def state_file(environ: Mapping[str, str] = os.environ, package: Path = PACKAGE) -> Path:
    """The file holding totals and history: wherever DASH_STATE says, else the user's data folder."""
    explicit = environ.get("DASH_STATE")
    if explicit:
        return Path(explicit)
    # Run straight from a copy of the source, the dashboard goes on using the data folder beside
    # it if there is one: that is where it kept its history before it could be installed.
    beside = package.parent / "data" / "state.json"
    if beside.exists():
        return beside
    return data_dir(environ=environ) / "state.json"
