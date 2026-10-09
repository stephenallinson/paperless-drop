"""The watched inbox folder and its processing / sent / failed subfolders."""
from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Callable

from .sender import Outcome, Status

log = logging.getLogger(__name__)

IGNORED_SUFFIXES = (".part", ".crdownload", ".download", ".tmp", ".partial", ".opdownload",
                    ".aria2", ".swp")


def unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = os.path.splitext(name)
    n = 1
    while True:
        candidate = directory / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


class Inbox:
    def __init__(self, root: Path, retention_days: int = 30, settle_seconds: float = 3.0,
                 clock: Callable[[], float] = time.time):
        self.root = Path(root)
        self.retention_days = retention_days
        self.settle_seconds = settle_seconds
        self._clock = clock
        self._seen: dict[str, tuple[int, int]] = {}

    @property
    def processing(self) -> Path:
        return self.root / ".processing"

    @property
    def failed(self) -> Path:
        return self.root / "failed"

    @property
    def sent(self) -> Path:
        return self.root / "sent"

    def ensure(self) -> None:
        for d in (self.root, self.processing, self.failed, self.sent):
            d.mkdir(parents=True, exist_ok=True)

    def contains(self, path: Path) -> bool:
        try:
            return path.resolve().parent == self.root.resolve()
        except OSError:
            return False

    def settled_files(self) -> list[Path]:
        """Files that are complete: unchanged since the previous call and not freshly modified."""
        current: dict[str, tuple[int, int]] = {}
        ready: list[tuple[float, Path]] = []
        now = self._clock()
        try:
            entries = list(os.scandir(self.root))
        except FileNotFoundError:
            self._seen = {}
            return []
        for entry in entries:
            name = entry.name
            if name.startswith(".") or name.lower().endswith(IGNORED_SUFFIXES):
                continue
            try:
                if entry.is_symlink() or not entry.is_file():
                    continue
                st = entry.stat()
            except OSError:
                continue
            sig = (st.st_size, st.st_mtime_ns)
            current[name] = sig
            if self._seen.get(name) == sig and now - st.st_mtime >= self.settle_seconds:
                ready.append((st.st_mtime, Path(entry.path)))
        self._seen = current
        return [p for _, p in sorted(ready)]

    def claim(self, path: Path) -> Path | None:
        """Atomically move a file into .processing/. None if someone else got it first."""
        self.processing.mkdir(parents=True, exist_ok=True)
        dest = unique_path(self.processing, path.name)
        try:
            os.rename(path, dest)
        except FileNotFoundError:
            return None  # the source vanished: another process claimed it
        return dest

    def unclaim(self, claimed: Path) -> Path:
        dest = unique_path(self.root, claimed.name)
        os.rename(claimed, dest)
        return dest

    def recover(self) -> int:
        """Return files stranded in .processing/ (after a crash) to the inbox."""
        if not self.processing.is_dir():
            return 0
        stranded = [p for p in self.processing.iterdir() if p.is_file()]
        for p in stranded:
            self.unclaim(p)
        return len(stranded)

    def archive(self, claimed: Path, outcome: Outcome) -> Path:
        if outcome.status is Status.FAILED:
            self.failed.mkdir(parents=True, exist_ok=True)
            dest = unique_path(self.failed, claimed.name)
            os.rename(claimed, dest)
            stamp = dt.datetime.fromtimestamp(self._clock()).isoformat(timespec="seconds")
            (dest.parent / f"{dest.name}.error.txt").write_text(f"{stamp}\n{outcome.message}\n")
            return dest
        day = dt.date.fromtimestamp(self._clock()).isoformat()
        folder = self.sent / day
        folder.mkdir(parents=True, exist_ok=True)
        dest = unique_path(folder, claimed.name)
        os.rename(claimed, dest)
        return dest

    def prune(self) -> list[Path]:
        """Delete sent/YYYY-MM-DD folders older than retention_days. 0 keeps everything."""
        if self.retention_days <= 0 or not self.sent.is_dir():
            return []
        cutoff = dt.date.fromtimestamp(self._clock()) - dt.timedelta(days=self.retention_days)
        removed = []
        for folder in self.sent.iterdir():
            try:
                day = dt.date.fromisoformat(folder.name)
            except ValueError:
                continue  # not ours; never touch it
            if folder.is_dir() and not folder.is_symlink() and day < cutoff:
                shutil.rmtree(folder)
                removed.append(folder)
        return removed
