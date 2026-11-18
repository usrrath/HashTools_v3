"""Streaming, memory-bounded multi-digest hashing shared by the Memory Capture tab.

Design
------
* One reader feeds immutable ``bytes`` windows to one worker thread per digest.
  ``hashlib`` releases the GIL for buffers over 2 KiB, so MD5 / SHA-1 / SHA-256 run on
  separate cores at the same time, and the reader keeps reading while earlier windows
  are still being hashed (I/O and hashing overlap).
* Queues are bounded, so memory stays at roughly ``(QUEUE_DEPTH + 2) * WINDOW`` no matter
  how large the image is (a 64 GiB image needs ~40 MiB, not 64 GiB).
* A window is shared by all digests (no per-digest copy).
* ``TailHasher`` hashes a file *while another process is still writing it*, which is what
  removes the long "hash after capture" wait of a conventional acquire-then-hash flow.
* Digests are packed onto ``min(#digests, #CPUs)`` worker threads, balanced by *measured*
  per-digest cost (``_algo_costs``). One thread per digest oversubscribes small machines:
  on 2 cores MD5+SHA-1+SHA-256 needed 3 runnable threads plus the reader.
* Evidence reads are page-cache friendly (``open_sequential`` / ``CacheTrimmer``): a RAM
  image is as large as RAM, so letting it pile up in the page cache both evicts the live
  system's memory (the thing being acquired) and makes a "verification" re-read come from
  RAM instead of from the disk the evidence actually sits on.
"""
from __future__ import annotations

import hashlib
import os
import queue
import threading
import time
from pathlib import Path

HASH_ALGORITHMS = ('md5', 'sha1', 'sha256')
DEFAULT_ALGORITHMS = ('md5', 'sha256')
# Measured on 2 vCPUs: 1-8 MiB windows are equivalent, 16 MiB is ~9% slower.
WINDOW = 4 * 1024 * 1024
QUEUE_DEPTH = 8
HEAD_GUARD = 1024 * 1024      # bytes of file head re-checked after streaming
_NEVER = lambda: False  # noqa: E731


def normalise_algorithms(algorithms):
    """Validate and de-duplicate, keeping the caller's order. md5/sha1/sha256 only."""
    chosen = tuple(dict.fromkeys(str(a).lower().replace('-', '') for a in algorithms))
    if not chosen:
        raise ValueError('Select at least one hash algorithm')
    bad = [a for a in chosen if a not in HASH_ALGORITHMS]
    if bad:
        raise ValueError('Unsupported hash algorithm: ' + ', '.join(bad))
    return chosen


_FADV = {k: getattr(os, 'POSIX_FADV_' + k, None) for k in ('SEQUENTIAL', 'DONTNEED')}
TRIM_BYTES = 128 * 1024 * 1024      # flush + evict consumed pages this often


def open_sequential(path):
    """Unbuffered read-only handle tuned for one forward pass over a huge file."""
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_SEQUENTIAL', 0)
    fd = os.open(path, flags)
    if _FADV['SEQUENTIAL'] is not None:
        try:
            os.posix_fadvise(fd, 0, 0, _FADV['SEQUENTIAL'])
        except OSError:
            pass            # advisory only: some filesystems / network mounts refuse it
    try:
        return os.fdopen(fd, 'rb', buffering=0)
    except BaseException:
        os.close(fd)
        raise


class CacheTrimmer:
    """Keep a streaming read from filling the page cache (Linux/POSIX; a no-op elsewhere).

    Every ``TRIM_BYTES`` consumed, dirty pages of the file are flushed (``fdatasync`` works on
    a read-only descriptor) and the consumed range is dropped with ``POSIX_FADV_DONTNEED``.
    Purely advisory: failures are ignored and never change what is read or hashed.
    """

    def __init__(self, handle, enabled=True):
        self.fd = handle.fileno()
        self.enabled = bool(enabled and _FADV['DONTNEED'] is not None and hasattr(os, 'fdatasync'))
        self._dropped = 0

    def update(self, done, final=False):
        if not self.enabled or (done - self._dropped < TRIM_BYTES and not final):
            return
        try:
            os.fdatasync(self.fd)
            os.posix_fadvise(self.fd, self._dropped, done - self._dropped, _FADV['DONTNEED'])
            self._dropped = done
        except OSError:
            self.enabled = False


_COSTS = {}


def _algo_costs():
    """Relative CPU seconds per byte of each digest on THIS machine (measured once, ~10 ms).

    SHA-NI hosts hash SHA-1/SHA-256 much faster than MD5; older CPUs invert that, so a fixed
    table would balance the worker threads badly."""
    if not _COSTS:
        probe = bytes(1024 * 1024)
        for a in HASH_ALGORITHMS:
            best = float('inf')
            for _ in range(3):
                h = hashlib.new(a)
                t = time.perf_counter()
                for _ in range(2):
                    h.update(probe)
                best = min(best, time.perf_counter() - t)
            _COSTS[a] = max(best, 1e-9)
    return _COSTS


def plan_workers(algorithms, cpus=None):
    """Greedy longest-processing-time packing of digests onto ``min(len, cpus)`` threads."""
    cpus = max(1, cpus or os.cpu_count() or 1)
    algorithms = tuple(algorithms)
    n = min(len(algorithms), cpus)
    if n >= len(algorithms):
        return [[a] for a in algorithms]
    costs = _algo_costs()
    bins = [[0.0, []] for _ in range(n)]
    for a in sorted(algorithms, key=lambda x: -costs[x]):
        lightest = min(bins, key=lambda b: b[0])
        lightest[0] += costs[a]
        lightest[1].append(a)
    return [b[1] for b in bins if b[1]]


class MultiHasher:
    """Feed ``bytes`` windows to several digests in parallel with bounded memory."""

    def __init__(self, algorithms=DEFAULT_ALGORITHMS, cpus=None):
        self.algorithms = normalise_algorithms(algorithms)
        self._digests = {a: hashlib.new(a) for a in self.algorithms}
        self.plan = plan_workers(self.algorithms, cpus)
        self._queues = [queue.Queue(QUEUE_DEPTH) for _ in self.plan]
        self._threads = [threading.Thread(target=self._worker, args=(group, q), daemon=True,
                                          name='hash-' + '+'.join(group))
                         for group, q in zip(self.plan, self._queues)]
        self.bytes_fed = 0
        self._closed = False
        for t in self._threads:
            t.start()

    def _worker(self, group, q):
        digests = [self._digests[a] for a in group]
        while True:
            chunk = q.get()
            if chunk is None:
                return
            for d in digests:
                d.update(chunk)

    def feed(self, chunk):
        if self._closed:
            raise ValueError('hasher already finished')
        for q in self._queues:       # blocks when the slowest worker lags: backpressure
            q.put(chunk)
        self.bytes_fed += len(chunk)

    def finish(self):
        """Flush all workers and return ``{algorithm: hexdigest}``. Idempotent."""
        if not self._closed:
            self._closed = True
            for q in self._queues:
                q.put(None)
            for t in self._threads:
                t.join()
        return {a: self._digests[a].hexdigest() for a in self.algorithms}

    close = finish   # on abort the result is simply discarded


class RateMeter:
    """Smoothed throughput and ETA. Cheap enough to call on every window."""

    def __init__(self, alpha=0.3):
        self._alpha, self._t, self._n, self.rate = alpha, time.monotonic(), 0, 0.0

    def update(self, done):
        now = time.monotonic()
        dt = now - self._t
        if dt >= 0.25:
            inst = (done - self._n) / dt
            self.rate = inst if self.rate == 0 else self._alpha * inst + (1 - self._alpha) * self.rate
            self._t, self._n = now, done
        return self.rate

    def eta(self, done, total):
        if not total or self.rate <= 0 or done >= total:
            return None
        return (total - done) / self.rate


def format_bytes(n):
    n = float(n)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or unit == 'TiB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.2f} {unit}'
        n /= 1024


def format_eta(seconds):
    if seconds is None:
        return '--:--'
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


def hash_file(path, algorithms=DEFAULT_ALGORITHMS, cancelled=_NEVER, report=None, phase='hash',
              window=WINDOW, evict=False):
    """Read ``path`` once (read-only) and return ``{algorithm: hexdigest}``.

    ``report`` receives ``{'phase', 'done', 'total', 'rate', 'eta'}`` about twice a second.
    Raises ``InterruptedError`` if ``cancelled()`` turns true. ``evict=True`` drops the pages
    already hashed from the page cache (use for RAM images; see :class:`CacheTrimmer`).
    """
    total = os.path.getsize(path)
    hasher = MultiHasher(algorithms)
    meter = RateMeter()
    last = 0.0
    done = 0
    try:
        with open_sequential(path) as f:
            trim = CacheTrimmer(f, evict)
            while True:
                if cancelled():
                    raise InterruptedError('Hashing cancelled; the image was not validated')
                chunk = f.read(window)
                if not chunk:
                    break
                hasher.feed(chunk)
                done += len(chunk)
                trim.update(done)
                now = time.monotonic()
                if report is not None and now - last >= 0.5:
                    last = now
                    meter.update(done)
                    report({'phase': phase, 'done': done, 'total': total,
                            'percent': (done * 100.0 / total) if total else 100.0,
                            'rate': meter.rate, 'eta': meter.eta(done, total)})
            trim.update(done, final=True)
    except BaseException:
        hasher.close()
        raise
    digests = hasher.finish()
    if report is not None:
        report({'phase': phase, 'done': done, 'total': total, 'percent': 100.0,
                'rate': meter.rate, 'eta': 0})
    return digests


class TailHasher(threading.Thread):
    """Hash a file that another process is still appending to.

    Bytes are consumed strictly in file order as they appear. Call :meth:`finish` once the
    writer has exited (drains what is left) or :meth:`abort` to discard. The digest is only
    trustworthy when the writer is sequential; :attr:`valid` turns False if the file ever
    shrinks below what was already hashed, and callers must still compare the final size
    with :attr:`done`. A later full re-read is the authoritative check.
    """

    def __init__(self, path, algorithms=DEFAULT_ALGORITHMS, window=WINDOW, evict=True):
        super().__init__(daemon=True, name='tail-hash')
        self.evict = evict
        self._head = hashlib.sha256()
        self._head_len = 0
        self.head = None            # (length, sha256) of the first HEAD_GUARD bytes as first seen
        self.path = Path(path)
        self.window = window
        self.hasher = MultiHasher(algorithms)
        self.done = 0
        self.valid = True
        self.error = None
        self.digests = None
        self._finish = threading.Event()
        self._abort = threading.Event()

    def request_finish(self):
        """Writer exited: tell the watcher to drain what is left and stop (non-blocking)."""
        self._finish.set()

    def finish(self):
        """Writer exited: drain remaining bytes, then return digests (None if aborted/error)."""
        self._finish.set()
        self.join()
        return self.digests

    def abort(self):
        self._abort.set()
        self._finish.set()
        self.join()

    def run(self):
        f = None
        try:
            while f is None:
                try:
                    f = open_sequential(self.path)
                except (FileNotFoundError, PermissionError):
                    # Not created yet, or the writer has not granted read sharing yet (Windows):
                    # keep retrying. Once the writer has exited the file opens normally and is
                    # hashed from byte 0, which is still exact.
                    if self._finish.is_set():
                        if self.path.exists():
                            raise          # genuinely unreadable: caller falls back to a re-read
                        break
                    time.sleep(0.05)
            if f is not None:
                self._drain(f)
        except BaseException as exc:      # never let the watcher thread die silently
            self.error = exc
            self.valid = False
        finally:
            if f is not None:
                f.close()
            if self._abort.is_set() or self.error is not None:
                self.hasher.close()
                self.digests = None
            else:
                self.digests = self.hasher.finish()

    def head_intact(self):
        """Cheap guard for non-sequential writers: are the first bytes still what we hashed
        while streaming? Tools that patch a header at the end (container formats) fail this
        immediately; it cannot prove the *middle* is unchanged, which only a re-read can."""
        if self.head is None:
            return False
        length, digest = self.head
        try:
            with open_sequential(self.path) as f:
                return hashlib.sha256(f.read(length)).hexdigest() == digest
        except OSError:
            return False

    def _note_head(self, chunk):
        if self._head_len < HEAD_GUARD:
            part = chunk[:HEAD_GUARD - self._head_len]
            self._head.update(part)
            self._head_len += len(part)

    def _drain(self, f):
        trim = CacheTrimmer(f, self.evict)
        try:
            self._drain_loop(f, trim)
        finally:
            if self.head is None and self._head_len:
                self.head = (self._head_len, self._head.hexdigest())
            trim.update(self.done, final=True)

    def _drain_loop(self, f, trim):
        while not self._abort.is_set():
            # Sample the flag BEFORE reading: if the writer had already exited, an empty
            # read is a true EOF; otherwise bytes may still arrive, so keep polling.
            writer_gone = self._finish.is_set()
            chunk = f.read(self.window)
            if chunk:
                self.hasher.feed(chunk)
                self._note_head(chunk)
                self.done += len(chunk)
                trim.update(self.done)
                continue
            if os.fstat(f.fileno()).st_size < self.done:
                self.valid = False          # file shrank/was truncated under us
            if writer_gone:
                return
            time.sleep(0.05)
