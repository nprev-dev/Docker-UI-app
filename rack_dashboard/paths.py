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
# The installers put the whole dashboard into one folder and leave a file of this name in it.
MARKER = ".rack-dashboard"


def home(environ: Mapping[str, str] = os.environ, prefix: str | Path | None = None) -> Path | None:
    """The folder an installer made for this copy, which then holds everything the dashboard
    writes as well. None for a copy that got here another way: pip, or run from the source."""
    given = environ.get("DASH_HOME")
    if given:
        return Path(given)
    # An installer puts the program's own Python in <folder>/tools/rack-dashboard.
    folder = Path(prefix if prefix is not None else sys.prefix).parent.parent
    return folder if (folder / MARKER).is_file() else None


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


def settings_file(environ: Mapping[str, str] = os.environ, prefix: str | Path | None = None) -> Path:
    """The file of KEY=value settings: in the dashboard's own folder if an installer made one,
    else in the user's settings folder."""
    own = home(environ, prefix)
    return (own if own else config_dir(environ=environ)) / "env"


def log_file(environ: Mapping[str, str] = os.environ, prefix: str | Path | None = None) -> Path:
    """Where output goes when there is no terminal to print it to."""
    own = home(environ, prefix)
    return (own if own else data_dir(environ=environ)) / f"{NAME}.log"


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


def state_file(environ: Mapping[str, str] = os.environ, package: Path = PACKAGE, prefix: str | Path | None = None) -> Path:
    """The file holding totals and history: wherever DASH_STATE says, else the dashboard's own
    folder if an installer made one, else the user's data folder."""
    explicit = environ.get("DASH_STATE")
    if explicit:
        return Path(explicit)
    own = home(environ, prefix)
    if own:
        return own / "state.json"
    # Run straight from a copy of the source, the dashboard goes on using the data folder beside
    # it if there is one: that is where it kept its history before it could be installed.
    beside = package.parent / "data" / "state.json"
    if beside.exists():
        return beside
    return data_dir(environ=environ) / "state.json"
