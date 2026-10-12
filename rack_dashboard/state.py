"""A small JSON file for the few things that must survive a restart."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class StateFile:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            # Missing or damaged: start empty rather than refuse to start.
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # Write beside the file and swap it in, so a crash never leaves half a file.
                scratch = self.path.with_suffix(self.path.suffix + ".tmp")
                scratch.write_text(json.dumps(self._data))
                os.replace(scratch, self.path)
            except OSError:
                # A full or read-only disk costs us history, not the dashboard.
                pass
