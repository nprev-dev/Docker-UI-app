"""What an install leaves on a machine outside the one folder it is meant to keep to.

    python tests/footprint.py note.json                     note down what is there now
    python tests/footprint.py note.json --allow PATH ...     say what has appeared since, and judge it

It looks five levels deep into the home folder, which covers the places programs keep settings,
data, caches and logs on Linux, macOS and Windows; at the shell's start-up files; and on Windows at
the user's PATH, the programs started at sign-in and the list of registered Pythons.

Other programs write to a home folder all the time, so not every new thing is ours. The check
fails for something new that bears the name of the dashboard or of the tools that install it and
is not under an allowed path, and for any change to a start-up file or to PATH.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

DEPTH = 5
# Names an install could leave behind: its own, and those of uv and the Python uv fetches.
OURS = ("rack-dashboard", "rack_dashboard", "astral", "cpython-")
STARTUP_FILES = (".profile", ".bash_profile", ".bash_login", ".bashrc", ".zshrc", ".zshenv", ".zprofile", ".config/fish/config.fish")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def ours(path: str) -> bool:
    for part in path.replace("\\", "/").lower().split("/"):
        if part in ("uv", "uv.exe", "uvx", "uvx.exe", "uvw.exe") or part.startswith("uv-") or any(name in part for name in OURS):
            return True
    return False


def listing(root: Path, depth: int = DEPTH) -> list[str]:
    found: list[str] = []

    def walk(folder: Path, left: int) -> None:
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for entry in entries:
            found.append(str(Path(entry.path).relative_to(root)).replace("\\", "/"))
            try:
                deeper = left > 1 and entry.is_dir(follow_symlinks=False)
            except OSError:
                deeper = False
            if deeper:
                walk(Path(entry.path), left - 1)

    walk(root, depth)
    return sorted(found)


def digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def registry() -> dict[str, str]:
    """On Windows: what is started at sign-in, the user's PATH and the Pythons registered for this user."""
    if sys.platform != "win32":
        return {}
    import winreg

    seen: dict[str, str] = {}

    def values(key_path: str, label: str, only: str | None = None) -> None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                for index in range(winreg.QueryInfoKey(key)[1]):
                    name, value, _ = winreg.EnumValue(key, index)
                    if only is None or name.lower() == only.lower():
                        seen[f"registry:{label}/{name}"] = str(value)
        except OSError:
            pass

    def subkeys(key_path: str, label: str, left: int) -> None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                names = [winreg.EnumKey(key, index) for index in range(winreg.QueryInfoKey(key)[0])]
        except OSError:
            return
        for name in names:
            seen[f"registry:{label}/{name}"] = ""
            if left > 1:
                subkeys(f"{key_path}\\{name}", f"{label}/{name}", left - 1)

    values(RUN_KEY, "Run")
    values("Environment", "Environment", only="Path")
    subkeys(r"Software\Python", "Python", 2)
    return seen


def note() -> dict:
    home = Path.home()
    return {
        "home": str(home),
        "paths": listing(home),
        "files": {name: digest(home / name) for name in STARTUP_FILES},
        "registry": registry(),
    }


def main(argv: list[str]) -> int:
    file = Path(argv[1])
    if not file.exists():
        file.write_text(json.dumps(note()))
        print(f"noted {len(json.loads(file.read_text())['paths'])} paths under {Path.home()}")
        return 0

    before, now = json.loads(file.read_text()), note()
    home = Path(now["home"])
    allowed = [value for index, value in enumerate(argv) if index > 0 and argv[index - 1] == "--allow"]

    def is_allowed(path: str) -> bool:
        if path.startswith("registry:"):
            return path in allowed
        full = os.path.normcase(str(home / path))
        return any(full == os.path.normcase(str(Path(prefix))) or full.startswith(os.path.normcase(str(Path(prefix))) + os.sep) for prefix in allowed)

    new = sorted(set(now["paths"]) - set(before["paths"]))
    new_registry = sorted(key for key in now["registry"] if key not in before["registry"])
    changed = [name for name in STARTUP_FILES if now["files"][name] != before["files"][name]]
    changed += [key for key in now["registry"] if key in before["registry"] and now["registry"][key] != before["registry"][key]]

    outside = [path for path in new + new_registry if not is_allowed(path)]
    problems = [path for path in outside if path.startswith("registry:") or ours(path)]

    print(f"new since the note: {len(new) + len(new_registry)}, of which outside the allowed paths: {len(outside)}")
    for path in outside[:80]:
        print("  ", "OURS " if path in problems else "other", path)
    if len(outside) > 80:
        print(f"   ... and {len(outside) - 80} more")
    for name in changed:
        print("   CHANGED", name)
    if problems or changed:
        print("FAIL  the install reached outside its folder")
        return 1
    print("ok    nothing of the install is outside its folder")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
