from __future__ import annotations

import hashlib
import math
import mmap
import os
import sqlite3
import stat as stat_module
import tempfile
import threading
import time
from contextlib import closing
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, wait

from PyQt6.QtCore import QObject, pyqtSignal

from classes.hash_result import ERROR, HashResult
from config import settings
from config.settings import HASH_CHUNK_SIZE

SUPPORTED_ALGORITHMS = ("md5", "sha1", "sha256", "sha512", "blake3")
DEFAULT_ALGORITHMS = SUPPORTED_ALGORITHMS


def format_duration(seconds):
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "Calculating..."
    seconds = int(math.ceil(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


class HashCancelled(Exception):
    """Raised inside a file hash when the user cancels; no digest is produced."""


_BLAKE3 = None


def _blake3_class():
    global _BLAKE3
    if _BLAKE3 is None:
        try:
            from blake3 import blake3
        except ImportError as exc:  # fail early and clearly, not once per file
            raise ValueError("BLAKE3 requires the 'blake3' package: pip install blake3") from exc
        _BLAKE3 = blake3
    return _BLAKE3


def validate_algorithms(algorithms) -> list[str]:
    """Lower-case, de-duplicate (order kept) and verify the algorithm names.

    Only algorithms that HashResult can store are accepted; anything else used to
    be computed and then silently thrown away.
    """
    return list(_validate(tuple(a.lower() for a in (algorithms or DEFAULT_ALGORITHMS))))


@lru_cache(maxsize=64)
def _validate(requested):
    names = tuple(dict.fromkeys(requested))
    for name in names:
        if name not in SUPPORTED_ALGORITHMS:
            raise ValueError(f"Unsupported hash algorithm: {name}")
    if "blake3" in names:
        _blake3_class()
    return names


# Shared by every hash_file call: one thread per algorithm (+1 read-ahead).
# hashlib and blake3 release the GIL, so these run truly in parallel.
_ALGO_POOL: ThreadPoolExecutor | None = None
_ALGO_POOL_LOCK = threading.Lock()
READ_BUFFER_SIZE = 8 * 1024 * 1024  # read() buffers: measured best at 4-8 MiB (16+ MiB falls out of cache)
PARALLEL_MIN_SIZE = 1024 * 1024  # below this, thread hand-off costs more than it saves


def _algo_pool() -> ThreadPoolExecutor:
    global _ALGO_POOL
    with _ALGO_POOL_LOCK:
        if _ALGO_POOL is None:
            _ALGO_POOL = ThreadPoolExecutor(max_workers=len(SUPPORTED_ALGORITHMS) + 1, thread_name_prefix="hash-algo")
        return _ALGO_POOL


def _new_digests(names, size):
    digests = {}
    for name in names:
        if name == "blake3":
            blake3 = _blake3_class()
            # Multi-threaded BLAKE3 only pays off on big inputs (pool start-up cost).
            threads = blake3.AUTO if size >= 4 * 1024 * 1024 else 1
            digests[name] = blake3(max_threads=threads)
        else:
            # usedforsecurity=False: MD5/SHA-1 are used as checksums here, and this
            # keeps them working on FIPS-enabled OpenSSL builds.
            digests[name] = hashlib.new(name, usedforsecurity=False)
    return digests


def _update_all(digests, view, parallel):
    if parallel and len(digests) > 1:
        pool = _algo_pool()
        futures = [pool.submit(d.update, view) for d in digests.values()]
        wait(futures)  # must finish before the buffer/mapping is released
        for future in futures:
            future.result()
    else:
        for digest in digests.values():
            digest.update(view)


def _windows_mmap(fd, size, chunk_size):
    """Yield zero-copy read-only views of the file in granularity-aligned windows.

    Shrinking of the file is re-checked before each window; a file that was
    truncated underneath us ends the iteration early (the caller then flags it).
    """
    gran = mmap.ALLOCATIONGRANULARITY
    window = max(gran, (chunk_size + gran - 1) // gran * gran)
    offset = 0
    while offset < size:
        length = min(window, size - offset)
        if os.fstat(fd).st_size < offset + length:
            return
        mapping = mmap.mmap(fd, length, access=mmap.ACCESS_READ, offset=offset)
        view = memoryview(mapping)
        try:
            if hasattr(mapping, "madvise") and hasattr(mmap, "MADV_SEQUENTIAL"):
                try:
                    mapping.madvise(mmap.MADV_SEQUENTIAL)
                except OSError:
                    pass
            yield view
        finally:
            view.release()
            mapping.close()
        offset += length


def _windows_read(handle, chunk_size, parallel):
    """Yield views of an unbuffered file using two buffers with read-ahead.

    The next block is read while the current one is being hashed, which hides
    device latency (network shares, USB, cold cache).
    """
    buffers = (bytearray(chunk_size), bytearray(chunk_size))
    pool = _algo_pool() if parallel else None
    current = 0
    count = handle.readinto(buffers[0])
    while count:
        pending = None
        if pool is not None:
            pending = pool.submit(handle.readinto, buffers[1 - current])
        view = memoryview(buffers[current])[:count]
        try:
            yield view
        finally:
            view.release()
        if pending is not None:
            count = pending.result()
        else:
            count = handle.readinto(buffers[1 - current])
        current = 1 - current


def hash_file(path, algorithms=None, chunk_size: int = HASH_CHUNK_SIZE, is_cancelled=lambda: False,
              on_bytes=None, parallel: bool | None = None) -> HashResult:
    """Hash one file in a single pass over its bytes.

    - Never returns digests for a cancelled or failed read (raises HashCancelled / OSError).
    - Sets `warning` if the file's size or mtime changed while it was read.
    - `parallel`: None = automatic (threads per algorithm for files >= 1 MiB);
      False = stay on the calling thread (used when files are already hashed concurrently).
    """
    path = os.fspath(path)
    names = validate_algorithms(algorithms)
    chunk_size = max(1, int(chunk_size))
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        before = os.fstat(fd)
        size = before.st_size
        is_regular = stat_module.S_ISREG(before.st_mode)
        if parallel is None:
            parallel = size >= PARALLEL_MIN_SIZE
        digests = _new_digests(names, size)
        if is_cancelled():
            raise HashCancelled()

        if is_regular and size <= min(chunk_size, settings.HASH_SMALL_FILE_LIMIT):
            # Small file: one read(), no threads, no mapping.
            if size:
                data = os.read(fd, size)
                if len(data) < size:  # short read (rare): keep reading until EOF/size
                    parts = [data]
                    remaining = size - len(data)
                    while remaining:
                        block = os.read(fd, remaining)
                        if not block:
                            break
                        parts.append(block)
                        remaining -= len(block)
                    data = b"".join(parts)
                for digest in digests.values():
                    digest.update(data)
                if on_bytes is not None:
                    on_bytes(len(data))
        else:
            done = False
            if is_regular and settings.HASH_USE_MMAP and size > 0 and "blake3" in digests:
                try:
                    with closing(_windows_mmap(fd, size, chunk_size)) as windows:
                        for view in windows:
                            if is_cancelled():
                                raise HashCancelled()
                            _update_all(digests, view, parallel)
                            if on_bytes is not None:
                                on_bytes(len(view))
                    done = True
                except (OSError, ValueError, OverflowError, BufferError):
                    # Mapping unsupported here (special FS, 32-bit, ...): redo with read().
                    digests = _new_digests(names, size)
                    os.lseek(fd, 0, os.SEEK_SET)
            if not done:
                # Fallback / non-regular files: buffered-free streaming to EOF.
                with os.fdopen(os.dup(fd), "rb", buffering=0) as handle:
                    with closing(_windows_read(handle, min(chunk_size, READ_BUFFER_SIZE), parallel)) as windows:
                        for view in windows:
                            if is_cancelled():
                                raise HashCancelled()
                            _update_all(digests, view, parallel)
                            if on_bytes is not None:
                                on_bytes(len(view))
        after = os.fstat(fd)
    finally:
        os.close(fd)

    warning = ""
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        warning = "File changed while it was being read; re-hash before relying on this value."
    hexes = {name: digest.hexdigest() for name, digest in digests.items()}
    return HashResult(
        file_name=os.path.basename(path),
        file_path=path,
        size=after.st_size,
        md5=hexes.get("md5", ""),
        sha1=hexes.get("sha1", ""),
        sha256=hexes.get("sha256", ""),
        sha512=hexes.get("sha512", ""),
        blake3=hexes.get("blake3", ""),
        success=True,
        warning=warning,
    )


def _error_result(path, exc) -> HashResult:
    return HashResult(file_name=os.path.basename(path), file_path=path, size=0, success=False,
                      error=str(exc), status=ERROR)


_INSERT_RESULT = ("INSERT INTO results(path,size,md5,sha1,sha256,sha512,blake3,success,error,warning) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)")


class HashWorker(QObject):
    """Bounded-memory traversal: on-disk manifest; files hashed in order, big files multi-threaded."""
    progress = pyqtSignal(int, int, int, str)
    file_completed = pyqtSignal(HashResult)
    results_batch = pyqtSignal(list)
    error = pyqtSignal(str)
    finished = pyqtSignal()
    database_ready = pyqtSignal(str)
    timing = pyqtSignal(str, str)  # elapsed, estimated remaining
    transfer = pyqtSignal(int, int, float, str)  # read bytes, total bytes, bytes/sec, current file

    def __init__(self, paths, algorithms=None):
        super().__init__()
        self.paths = tuple(paths)
        self.algorithms = validate_algorithms(algorithms)
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._cancelled = False
        self.result_db_path = None
        self._paused_at = None
        self._paused_total = 0.0
        self._timing_lock = threading.Lock()
        self._read_bytes = 0

    def cancel(self):
        self._cancelled = True
        self._stop.set()
        self._pause.clear()

    def pause(self):
        with self._timing_lock:
            if not self._pause.is_set():
                self._paused_at = time.monotonic()
                self._pause.set()

    def resume(self):
        with self._timing_lock:
            if self._paused_at is not None:
                self._paused_total += time.monotonic() - self._paused_at
                self._paused_at = None
            self._pause.clear()

    def _active_elapsed(self, started):
        with self._timing_lock:
            paused = self._paused_total
            if self._paused_at is not None:
                paused += time.monotonic() - self._paused_at
        return max(0.0, time.monotonic() - started - paused)

    def _wait_if_paused(self):
        while self._pause.is_set() and not self._stop.is_set():
            self._stop.wait(0.1)
        return self._stop.is_set()

    def run(self):
        """One directory traversal; persist manifest/results on disk; bounded UI signals.

        Counting is necessary for an honest percentage. An on-disk manifest avoids
        repeating slow directory traversal and permits deterministic progress.
        Files are hashed one after another (sequential disk access; Python threads
        per tiny file measured 2-3x SLOWER because of GIL hand-off); parallelism
        lives inside hash_file for large files.
        """
        from utils.file_utils import iter_files
        db = None
        batch = []
        processed = 0
        total = 0
        total_bytes = 0
        cancelled = False
        job_started = time.monotonic()
        try:
            fd, db_path = tempfile.mkstemp(prefix="hashtools_job_", suffix=".sqlite3")
            os.close(fd)
            self.result_db_path = db_path
            db = sqlite3.connect(db_path)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA temp_store=FILE")
            db.execute("CREATE TABLE manifest (id INTEGER PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL)")
            db.execute("CREATE TABLE results (id INTEGER PRIMARY KEY, path TEXT, size INTEGER, md5 TEXT, sha1 TEXT, sha256 TEXT, sha512 TEXT, blake3 TEXT, success INTEGER, error TEXT, warning TEXT)")
            db.commit()
            self.database_ready.emit(db_path)
            self.progress.emit(0, 0, -1, "Indexing selected files (counting)...")
            pending = []
            last_emit = time.monotonic()
            for path in iter_files(self.paths):
                if self._wait_if_paused():
                    cancelled = True
                    break
                total += 1
                try:
                    size = max(0, os.stat(path).st_size)
                except OSError:
                    size = 0
                total_bytes += size
                pending.append((path, size))
                if len(pending) >= 8192:
                    db.executemany("INSERT INTO manifest(path,size) VALUES (?,?)", pending)
                    pending.clear()
                    db.commit()
                    self.results_batch.emit([])  # notify UI of newly committed rows
                now = time.monotonic()
                if now - last_emit >= .25:
                    self.progress.emit(total, 0, -1, f"Indexing: {total:,} files found")
                    self.timing.emit(format_duration(self._active_elapsed(job_started)), "Counting files...")
                    last_emit = now
            if pending and not cancelled:
                db.executemany("INSERT INTO manifest(path,size) VALUES (?,?)", pending)
            db.commit()
            if cancelled:
                self.progress.emit(0, total, -1, "Cancelled during indexing")
                return
            if total == 0:
                self.progress.emit(0, 0, 100, "No files found")
                return

            hashing_started = self._active_elapsed(job_started)
            result_rows = []
            current_path = ""
            last_tick = 0.0

            def tick(force=False):
                """Emit progress/transfer/timing signals (run thread only), at most 5x/second."""
                nonlocal last_tick
                now = time.monotonic()
                if not force and now - last_tick < .2:
                    return
                last_tick = now
                read_bytes = self._read_bytes
                active = self._active_elapsed(job_started)
                elapsed = max(0.001, active - hashing_started)
                self.transfer.emit(read_bytes, total_bytes, read_bytes / elapsed, current_path)
                # Percent and ETA follow BYTES, not file count: one 100 GB image among
                # thousands of tiny files would otherwise make both meaningless.
                if total_bytes > 0:
                    fraction = min(1.0, read_bytes / total_bytes)
                    remaining = elapsed / read_bytes * (total_bytes - read_bytes) if read_bytes and elapsed >= 2 else None
                else:
                    fraction = processed / total
                    remaining = elapsed / processed * (total - processed) if processed >= 5 and elapsed >= 2 else None
                percent = min(100, int(fraction * 100))
                self.progress.emit(processed, total, percent, f"Hashed {processed:,}/{total:,} files ({percent}%)")
                self.timing.emit(format_duration(active), format_duration(remaining))

            def on_bytes_inline(amount):
                self._read_bytes += amount
                tick()

            def record(path, result):
                nonlocal processed, batch
                processed += 1
                result_rows.append((path, result.size, result.md5, result.sha1, result.sha256, result.sha512,
                                    result.blake3, int(result.success), result.error, result.warning))
                if processed <= 5000:
                    batch.append(result)
                    if len(batch) >= 100:
                        self.results_batch.emit(batch)
                        batch = []
                if len(result_rows) >= 512:
                    db.executemany(_INSERT_RESULT, result_rows)
                    result_rows.clear()
                    db.commit()
                    self.results_batch.emit([])  # notify UI of newly committed rows
                tick(force=processed == total)

            last_id = 0
            while not cancelled:
                # Keyset pagination: no cursor is kept open across commits.
                page = db.execute("SELECT id,path FROM manifest WHERE id>? ORDER BY id LIMIT 4096", (last_id,)).fetchall()
                if not page:
                    break
                for row_id, path in page:
                    last_id = row_id
                    if self._wait_if_paused():
                        cancelled = True
                        break
                    current_path = path
                    try:
                        result = hash_file(path, self.algorithms, is_cancelled=self._wait_if_paused,
                                           on_bytes=on_bytes_inline)
                    except HashCancelled:
                        cancelled = True
                        break
                    except Exception as exc:  # one unreadable/odd file must not abort the whole job
                        result = _error_result(path, exc)
                    record(path, result)

            tick(force=True)
            if batch:
                self.results_batch.emit(batch)
            if result_rows:
                db.executemany(_INSERT_RESULT, result_rows)
                db.commit()
                self.results_batch.emit([])
            db.execute("DROP TABLE manifest")
            db.commit()
            self.timing.emit(format_duration(self._active_elapsed(job_started)), "Cancelled" if cancelled else "0s")
            if cancelled:
                self.progress.emit(processed, total, -1, f"Cancelled after {processed:,} files")
            else:
                self.progress.emit(processed, total, 100, f"Completed {processed:,} files")
        except Exception as exc:
            self.error.emit(f"Hash job failed: {exc}")
        finally:
            if db is not None:
                db.close()
            self.finished.emit()
