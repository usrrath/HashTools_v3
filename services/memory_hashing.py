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


class MultiHasher:
    """Feed ``bytes`` windows to several digests in parallel with bounded memory."""

    def __init__(self, algorithms=DEFAULT_ALGORITHMS):
        self.algorithms = normalise_algorithms(algorithms)
        self._digests = {a: hashlib.new(a) for a in self.algorithms}
        self._queues = {a: queue.Queue(QUEUE_DEPTH) for a in self.algorithms}
        self._threads = [threading.Thread(target=self._worker, args=(a,), daemon=True,
                                          name=f'hash-{a}') for a in self.algorithms]
        self.bytes_fed = 0
        self._closed = False
        for t in self._threads:
            t.start()

    def _worker(self, algo):
        digest, q = self._digests[algo], self._queues[algo]
        while True:
            chunk = q.get()
            if chunk is None:
                return
            digest.update(chunk)

    def feed(self, chunk):
        if self._closed:
            raise ValueError('hasher already finished')
        for q in self._queues.values():       # blocks when the slowest digest lags: backpressure
            q.put(chunk)
        self.bytes_fed += len(chunk)

    def finish(self):
        """Flush all workers and return ``{algorithm: hexdigest}``. Idempotent."""
        if not self._closed:
            self._closed = True
            for q in self._queues.values():
                q.put(None)
            for t in self._threads:
                t.join()
        return {a: d.hexdigest() for a, d in self._digests.items()}

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
              window=WINDOW):
    """Read ``path`` once (read-only) and return ``{algorithm: hexdigest}``.

    ``report`` receives ``{'phase', 'done', 'total', 'rate', 'eta'}`` about twice a second.
    Raises ``InterruptedError`` if ``cancelled()`` turns true.
    """
    total = os.path.getsize(path)
    hasher = MultiHasher(algorithms)
    meter = RateMeter()
    last = 0.0
    done = 0
    try:
        with open(path, 'rb', buffering=0) as f:
            while True:
                if cancelled():
                    raise InterruptedError('Hashing cancelled; the image was not validated')
                chunk = f.read(window)
                if not chunk:
                    break
                hasher.feed(chunk)
                done += len(chunk)
                now = time.monotonic()
                if report is not None and now - last >= 0.5:
                    last = now
                    meter.update(done)
                    report({'phase': phase, 'done': done, 'total': total,
                            'percent': (done * 100.0 / total) if total else 100.0,
                            'rate': meter.rate, 'eta': meter.eta(done, total)})
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

    def __init__(self, path, algorithms=DEFAULT_ALGORITHMS, window=WINDOW):
        super().__init__(daemon=True, name='tail-hash')
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
                    f = open(self.path, 'rb', buffering=0)
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

    def _drain(self, f):
        while not self._abort.is_set():
            # Sample the flag BEFORE reading: if the writer had already exited, an empty
            # read is a true EOF; otherwise bytes may still arrive, so keep polling.
            writer_gone = self._finish.is_set()
            chunk = f.read(self.window)
            if chunk:
                self.hasher.feed(chunk)
                self.done += len(chunk)
                continue
            if os.fstat(f.fileno()).st_size < self.done:
                self.valid = False          # file shrank/was truncated under us
            if writer_gone:
                return
            time.sleep(0.05)
