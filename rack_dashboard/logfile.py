"""A log file that cannot fill the disk."""

from __future__ import annotations

import io
import os
import threading
from pathlib import Path


class CappedLog(io.TextIOBase):
    """Text is added at the end of the file. Once the file is larger than `limit` bytes, the older
    half of it is dropped, at the start of a line."""

    def __init__(self, path: str | Path, limit: int = 1_000_000):
        self._path = Path(path)
        self._limit = limit
        self._lock = threading.Lock()
        self._file = self._open()
        self._size = self._path.stat().st_size

    def _open(self):
        return open(self._path, "a", encoding="utf-8", errors="replace")

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        with self._lock:
            try:
                self._file.write(text)
                self._file.flush()
                self._size += len(text.encode("utf-8", "replace"))
                if self._size > self._limit:
                    self._trim()
            except (OSError, ValueError):
                # A full disk, or a file taken away: losing a log line must never stop the dashboard.
                pass
        return len(text)

    def _trim(self) -> None:
        self._file.close()
        with open(self._path, "rb") as whole:
            whole.seek(-min(self._limit // 2, os.path.getsize(self._path)), os.SEEK_END)
            kept = whole.read()
        # What is kept starts in the middle of a line (perhaps of a character); drop that piece.
        kept = kept[kept.find(b"\n") + 1 :]
        with open(self._path, "wb") as whole:
            whole.write(kept)
        self._file = self._open()
        self._size = len(kept)

    def flush(self) -> None:
        with self._lock:
            try:
                self._file.flush()
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        with self._lock:
            self._file.close()
        super().close()
