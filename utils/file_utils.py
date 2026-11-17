from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Iterable


def human_size(size: int) -> str:
    value = float(size)
    for unit in ["bytes", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{value:,.0f} {unit}" if unit == "bytes" else f"{value:,.2f} {unit}"
        value /= 1024
    return f"{size:,} bytes"


def guess_file_type(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    suffix = Path(path).suffix.upper().lstrip(".") or "Unknown"
    return f"{suffix}" + (f" ({mime})" if mime else "")


IGNORED_SYSTEM_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
IGNORED_SYSTEM_DIRS = {"__MACOSX", ".git", ".svn", ".hg", "__pycache__"}


def _is_ignored_system_file(path: Path, root: Path | None = None) -> bool:
    """True for OS/cache files and for files inside OS/cache folders.

    Folder names are tested only BELOW `root` (the folder the user selected), so
    evidence that lives under a parent called e.g. ".git" is not silently skipped.
    """
    if path.name in IGNORED_SYSTEM_FILES:
        return True
    if root is not None:
        try:
            parts = path.relative_to(root).parts[:-1]
        except ValueError:
            parts = ()
        return any(part in IGNORED_SYSTEM_DIRS for part in parts)
    return False


def collect_files(paths: Iterable[str | Path], recursive: bool = True) -> list[str]:
    """Return readable user files from selected files/folders, sorted and de-duplicated.

    Uses os.walk (prunes ignored folders without entering them, does not follow
    directory symlinks). Call it ONCE per selection and pass the result on;
    UI tabs accept `scanned=True` to avoid re-walking the folder.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(candidate: Path) -> None:
        try:
            key = os.path.realpath(candidate)
            if key not in seen and os.path.isfile(key):
                seen.add(key)
                found.append(key)
        except OSError:
            pass

    for raw in paths:
        p = Path(raw).expanduser()
        try:
            if p.is_file():
                if p.name not in IGNORED_SYSTEM_FILES:
                    add(p)
            elif p.is_dir():
                if recursive:
                    for dirpath, dirnames, filenames in os.walk(p, followlinks=False):
                        dirnames[:] = [d for d in dirnames if d not in IGNORED_SYSTEM_DIRS]
                        for name in filenames:
                            if name not in IGNORED_SYSTEM_FILES:
                                add(Path(dirpath) / name)
                else:
                    for entry in p.iterdir():
                        if entry.is_file() and entry.name not in IGNORED_SYSTEM_FILES:
                            add(entry)
        except OSError:
            continue
    return sorted(found, key=lambda x: (Path(x).name.lower(), x))


def iter_files(paths, recursive=True):
    """Bounded-memory file traversal; avoids sorting and global deduplication."""
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_file():
            if path.name not in IGNORED_SYSTEM_FILES:
                yield str(path)
        elif path.is_dir():
            for directory, dirs, names in os.walk(path, followlinks=False):
                dirs[:] = [d for d in dirs if d not in IGNORED_SYSTEM_DIRS]
                for name in names:
                    if name not in IGNORED_SYSTEM_FILES:
                        candidate = os.path.join(directory, name)
                        if not os.path.islink(candidate):
                            yield candidate
                if not recursive:
                    dirs.clear()
