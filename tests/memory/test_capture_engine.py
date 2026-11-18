"""Tests for the memory-capture engine improvements: balanced hashing workers, head guard
for streamed hashes, no wasted streaming for container formats, cache trimming, stall warning."""
import hashlib
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from services import memory_hashing as mh
from services import physical_memory_backend as pmb
from tests.test_physical_capture import write_fake_tool

# Writes sequentially, then rewrites the first bytes at the end, like a container finaliser.
REWRITE_HEAD = r'''#!{python}
import os, sys, time
out = sys.argv[1]; size = int(os.environ.get("FAKE_SIZE", "4194304"))
with open(out, "xb") as f:
    f.write(bytes(range(256)) * (size // 256)); f.flush()
    time.sleep(0.8)          # let the streaming hasher read the original header first
    f.seek(0); f.write(b"HEADER-REWRITTEN!")
'''


class WorkerPlanTests(unittest.TestCase):
    def test_one_thread_per_digest_when_cpus_allow(self):
        self.assertEqual(len(mh.plan_workers(('md5', 'sha1', 'sha256'), cpus=8)), 3)

    def test_never_more_threads_than_cpus_and_every_digest_assigned_once(self):
        for cpus in (1, 2, 3):
            plan = mh.plan_workers(('md5', 'sha1', 'sha256'), cpus=cpus)
            self.assertLessEqual(len(plan), cpus)
            self.assertEqual(sorted(a for g in plan for a in g), ['md5', 'sha1', 'sha256'])

    def test_digests_exact_for_every_cpu_count(self):
        data = os.urandom(9 * 1024 * 1024 + 5)
        want = {a: hashlib.new(a, data).hexdigest() for a in mh.HASH_ALGORITHMS}
        for cpus in (1, 2, 3, 8):
            h = mh.MultiHasher(mh.HASH_ALGORITHMS, cpus=cpus)
            for i in range(0, len(data), 1 << 20):
                h.feed(data[i:i + (1 << 20)])
            self.assertEqual(h.finish(), want, cpus)


class FileHashTests(unittest.TestCase):
    def test_evict_and_sequential_open_do_not_change_digest(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'a.raw'
            data = os.urandom(5 * 1024 * 1024 + 1)
            p.write_bytes(data)
            with mock.patch.object(mh, 'TRIM_BYTES', 1024 * 1024):   # force several trims
                r = mh.hash_file(p, mh.HASH_ALGORITHMS, evict=True, window=512 * 1024)
            self.assertEqual(r['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(r['md5'], hashlib.md5(data).hexdigest())

    def test_trimmer_failures_are_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'a.raw'
            p.write_bytes(b'x' * 4096)
            with mock.patch.object(mh.os, 'posix_fadvise', side_effect=OSError), \
                 mock.patch.object(mh, 'TRIM_BYTES', 1):
                self.assertEqual(mh.hash_file(p, ('sha256',), evict=True)['sha256'],
                                 hashlib.sha256(b'x' * 4096).hexdigest())


class TailHasherGuardTests(unittest.TestCase):
    def run_tail(self, rewrite):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'img'
            p.write_bytes(os.urandom(3 * 1024 * 1024))
            t = mh.TailHasher(p, ('sha256',))
            t.start()
            time.sleep(0.3)
            if rewrite:
                with open(p, 'r+b') as f:
                    f.write(b'PATCHED-HEADER')
            t.finish()
            return t.head_intact(), t.done

    def test_head_intact_for_sequential_writer(self):
        ok, done = self.run_tail(rewrite=False)
        self.assertTrue(ok)
        self.assertEqual(done, 3 * 1024 * 1024)

    def test_rewritten_head_is_detected(self):
        ok, _ = self.run_tail(rewrite=True)
        self.assertFalse(ok)


class CaptureEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.out = self.root / 'ev'
        self.out.mkdir()
        self.tool = write_fake_tool(self.bin, 'avml')
        self.patches = [
            mock.patch.dict(os.environ, {'PATH': f'{self.bin}{os.pathsep}{os.environ["PATH"]}'}),
            mock.patch.object(pmb.platform, 'system', return_value='Linux'),
            mock.patch.object(pmb.platform, 'machine', return_value='x86_64'),
            mock.patch.object(pmb, 'is_elevated', return_value=True),
            mock.patch.object(pmb.psutil, 'virtual_memory', return_value=mock.Mock(total=4 * 1024 ** 2)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def capture(self, **kw):
        return pmb.capture_memory('AVML', str(self.tool), str(self.out / 'm.lime'), **kw)

    def test_header_rewriting_tool_never_yields_a_wrong_digest_even_without_verify(self):
        self.tool.write_text(REWRITE_HEAD.format(python=sys.executable))
        r = self.capture(verify=False)
        truth = hashlib.sha256((self.out / 'm.lime').read_bytes()).hexdigest()
        self.assertEqual(r['sha256'], truth)
        self.assertEqual(r['hash_source'], 're-read')
        self.assertTrue(any('header was rewritten' in w for w in r['warnings']))

    def test_container_format_skips_streaming_thread(self):
        with mock.patch.dict(pmb.TOOLS['AVML'], {'streamable': False}), \
             mock.patch.object(pmb, 'TailHasher', side_effect=AssertionError('must not stream')):
            r = self.capture()
        data = (self.out / 'm.lime').read_bytes()
        self.assertEqual(r['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(r['hash_source'], 're-read')
        self.assertIn('full read of the finished image', (self.out / 'm.lime.summary.txt').read_text())

    def test_sequential_tool_still_streams_and_verifies(self):
        r = self.capture()
        self.assertTrue(r['verified'])
        self.assertEqual(r['hash_source'], 'streamed, confirmed by re-read')

    def test_sidecars_match_image(self):
        r = self.capture()
        data = (self.out / 'm.lime').read_bytes()
        self.assertEqual((self.out / 'm.lime.sha256').read_text().split()[0], hashlib.sha256(data).hexdigest())
        self.assertEqual(r['sha256'], hashlib.sha256(data).hexdigest())

    def test_stall_warning_is_reported_but_tool_is_not_killed(self):
        stop, msgs = threading.Event(), []
        threading.Timer(2.0, stop.set).start()
        with mock.patch.object(pmb, 'STALL_WARNING_SECONDS', 0.6), \
             mock.patch.dict(os.environ, {'FAKE_MODE': 'hang'}):
            with self.assertRaises(InterruptedError):
                self.capture(cancelled=stop.is_set, progress=msgs.append)
        stalled = [m for m in msgs if 'has not changed' in m]
        self.assertEqual(len(stalled), 1, msgs)          # reported once, not every tick
        self.assertIn('NOT being stopped', stalled[0])


if __name__ == '__main__':
    unittest.main()
