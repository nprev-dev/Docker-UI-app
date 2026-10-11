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


def config_dir(platform: str = sys.platform, environ: Mapping[str, str] = os.environ, home: Path | None = None) -> Path:
    """The user's own folder for an application's settings."""
    home = home or Path.home()
    if platform == "win32":
        base = Path(environ.get("APPDATA") or home / "AppData" / "Roaming")
    else:
        # macOS programs run from a terminal keep their settings here too, as Linux ones do.
        given = environ.get("XDG_CONFIG_HOME", "")
        base = Path(given) if given.startswith("/") and platform != "darwin" else home / ".config"
    return base / NAME


def read_settings(path: Path) -> dict[str, str]:
    """The KEY=value lines of a settings file. Blank lines and lines starting with # are skipped,
    and so is anything else that is not a setting; a missing file is no settings at all."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return {}
    settings = {}
    for line in lines:
        key, found, value = line.strip().partition("=")
        key, value = key.strip(), value.strip()
        if not found or not key or key.startswith("#") or not key.replace("_", "").isalnum():
            continue
        # Quotes around a value are how people write one with spaces; they are not part of it.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        settings[key] = value
    return settings


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
