import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest

from blake3 import blake3
from config import settings
from services.hash_service import HashCancelled, HashWorker, hash_file, validate_algorithms


class HashingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, name, data):
        p = self.root / name
        p.write_bytes(data)
        return p

    def test_known_vectors(self):
        for name, data in (("empty", b""), ("abc", b"abc")):
            r = hash_file(self.make(name, data))
            for algo in ("md5", "sha1", "sha256", "sha512"):
                self.assertEqual(getattr(r, algo), hashlib.new(algo, data).hexdigest())
            self.assertTrue(r.success)
            self.assertEqual(r.status, "OK")

    def test_multi_chunk_matches_hashlib(self):
        data = os.urandom(3_500_000)
        r = hash_file(self.make("big", data), chunk_size=64 * 1024)
        self.assertEqual(r.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(r.size, len(data))

    def test_cancel_never_returns_digest(self):
        p = self.make("c", os.urandom(1_000_000))
        with self.assertRaises(HashCancelled):
            hash_file(p, is_cancelled=lambda: True)
        calls = {"n": 0}

        def cancel_mid():
            calls["n"] += 1
            return calls["n"] > 3
        with self.assertRaises(HashCancelled):
            hash_file(p, chunk_size=1024, is_cancelled=cancel_mid)

    def test_unreadable_missing_file(self):
        with self.assertRaises(OSError):
            hash_file(self.root / "nope.bin")

    def test_file_changed_during_read_is_flagged(self):
        p = self.make("g", b"a" * 4096)
        state = {"done": False}

        def grow():
            if not state["done"]:
                state["done"] = True
                with open(p, "ab") as h:
                    h.write(b"more")
                os.utime(p, ns=(time.time_ns(), time.time_ns() + 10_000_000_000))
            return False
        r = hash_file(p, chunk_size=1024, is_cancelled=grow)
        self.assertTrue(r.warning)

    def test_bad_algorithm(self):
        with self.assertRaises(ValueError):
            validate_algorithms(["md5", "sha999"])

    def _expect(self, data):
        return {a: hashlib.new(a, data).hexdigest() for a in ("md5", "sha1", "sha256", "sha512")} | {"blake3": blake3(data).hexdigest()}

    def test_all_algorithms_at_size_boundaries(self):
        """Every code path (single read, mmap windows, read() fallback) must give identical digests."""
        chunk = 64 * 1024
        sizes = [0, 1, 63, 64, 65, 1023, 1024, 1025, chunk - 1, chunk, chunk + 1,
                 2 * chunk, 2 * chunk + 7, settings.HASH_SMALL_FILE_LIMIT + 1, 5 * 1024 * 1024 + 3]
        original = settings.HASH_USE_MMAP
        try:
            for use_mmap in (True, False):
                settings.HASH_USE_MMAP = use_mmap
                for size in sizes:
                    data = os.urandom(size)
                    path = self.make(f"b{size}", data)
                    for parallel in (None, False):
                        r = hash_file(path, chunk_size=chunk, parallel=parallel)
                        for algo, expected in self._expect(data).items():
                            self.assertEqual(getattr(r, algo), expected, (algo, size, use_mmap, parallel))
                        self.assertEqual(r.size, size)
                        self.assertEqual(r.warning, "")
        finally:
            settings.HASH_USE_MMAP = original

    def test_blake3_known_vector(self):
        # Official BLAKE3 test vector for empty input (what `b3sum` prints).
        r = hash_file(self.make("e", b""), ["blake3"])
        self.assertEqual(r.blake3, "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262")
        self.assertEqual(r.md5, "")

    def test_single_algorithm_leaves_others_empty(self):
        r = hash_file(self.make("s", b"abc"), ["sha256"])
        self.assertTrue(r.sha256)
        self.assertEqual((r.md5, r.sha1, r.sha512, r.blake3), ("", "", "", ""))

    def test_duplicates_and_case_are_normalised(self):
        self.assertEqual(validate_algorithms(["SHA256", "sha256", "MD5"]), ["sha256", "md5"])
        with self.assertRaises(ValueError):
            validate_algorithms(["sha3_256"])  # HashResult cannot store it

    def test_truncated_file_is_flagged_not_crashed(self):
        p = self.make("t", os.urandom(3_000_000))
        state = {"n": 0}

        def truncate():
            state["n"] += 1
            if state["n"] == 2:
                os.truncate(p, 100_000)
                os.utime(p, ns=(time.time_ns(), time.time_ns() + 10_000_000_000))
            return False
        r = hash_file(p, ["blake3"], chunk_size=64 * 1024, is_cancelled=truncate)
        self.assertTrue(r.warning)

    def test_fifo_streams_without_mmap(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("no FIFOs on this platform")
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        data = os.urandom(300_000)
        import threading

        def writer():
            with open(fifo, "wb") as h:
                h.write(data)
        t = threading.Thread(target=writer)
        t.start()
        r = hash_file(fifo, chunk_size=64 * 1024)
        t.join()
        self.assertEqual(r.sha256, hashlib.sha256(data).hexdigest())

    def run_worker(self, folder, algorithms=None, hook=None):
        import sqlite3
        w = HashWorker([str(folder)], algorithms)
        errors = []
        w.error.connect(errors.append)
        if hook:
            hook(w)
        w.run()
        self.assertEqual(errors, [])
        db = sqlite3.connect(w.result_db_path)
        rows = db.execute("SELECT path,size,md5,sha1,sha256,sha512,blake3,success,error,warning FROM results ORDER BY id").fetchall()
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        db.close()
        os.remove(w.result_db_path)
        return rows, tables

    def test_worker_results_match_and_keep_manifest_order(self):
        folder = self.root / "tree"
        (folder / "sub").mkdir(parents=True)
        expected = {}
        for i in range(60):
            data = os.urandom((i * 7919) % 90_000)
            p = folder / ("sub" if i % 2 else ".") / f"f{i:03d}.bin"
            p.write_bytes(data)
            expected[str(p)] = data
        (folder / "big.bin").write_bytes(os.urandom(5_000_000))
        expected[str(folder / "big.bin")] = (folder / "big.bin").read_bytes()
        (folder / ".DS_Store").write_bytes(b"ignored")
        rows, tables = self.run_worker(folder)
        self.assertNotIn("manifest", tables)
        self.assertEqual(len(rows), len(expected))
        for path, size, md5, sha1, sha256, sha512, b3, ok, err, warn in rows:
            data = expected[path]
            self.assertEqual((size, md5, sha256, b3, ok), (len(data), hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest(), blake3(data).hexdigest(), 1))
        from utils.file_utils import iter_files
        self.assertEqual([r[0] for r in rows], list(iter_files([str(folder)])))

    def test_worker_unreadable_file_becomes_error_row(self):
        import services.hash_service as hs
        folder = self.root / "err"
        folder.mkdir()
        (folder / "ok.bin").write_bytes(b"ok")
        (folder / "locked.bin").write_bytes(b"x")
        real = hs.hash_file

        def flaky(path, *a, **k):
            if path.endswith("locked.bin"):
                raise PermissionError("denied")
            return real(path, *a, **k)
        hs.hash_file = flaky
        try:
            rows, _ = self.run_worker(folder)
        finally:
            hs.hash_file = real
        by_name = {os.path.basename(r[0]): r for r in rows}
        self.assertEqual(by_name["ok.bin"][7], 1)
        self.assertEqual(by_name["locked.bin"][7], 0)
        self.assertIn("denied", by_name["locked.bin"][8])

    def test_worker_cancel_records_only_completed_prefix(self):
        import services.hash_service as hs
        folder = self.root / "cancel"
        folder.mkdir()
        for i in range(40):
            (folder / f"f{i:02d}.bin").write_bytes(os.urandom(4096))
        real = hs.hash_file
        calls = {"n": 0}
        holder = {}

        def counting(*a, **k):
            result = real(*a, **k)
            calls["n"] += 1
            if calls["n"] == 5:
                holder["w"].cancel()
            return result
        hs.hash_file = counting
        try:
            rows, _ = self.run_worker(folder, hook=lambda w: holder.update(w=w))
        finally:
            hs.hash_file = real
        from utils.file_utils import iter_files
        self.assertEqual([r[0] for r in rows], list(iter_files([str(folder)]))[:5])


if __name__ == "__main__":
    unittest.main()
