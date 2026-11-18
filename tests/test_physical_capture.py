"""Tests for the external-tool physical RAM acquisition backend.

They use a stand-in executable named like the real tool, so the process lifecycle,
logging, cancellation, hashing and error handling run for real. Cross-platform
detection is tested by patching platform.system()/machine(). No real acquisition
is attempted.
"""
import hashlib
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from services import physical_memory_backend as pmb

FAKE_AVML = r'''#!{python}
import os, sys, time
out = sys.argv[1]
mode = os.environ.get("FAKE_MODE", "ok")
size = int(os.environ.get("FAKE_SIZE", "4194304"))
if mode == "fail":
    open(out, "wb").write(b"partial"); print("boom"); sys.exit(3)
if mode == "empty":
    open(out, "xb").close(); sys.exit(0)
if mode == "hang":
    with open(out, "xb") as f:
        f.write(b"x" * 1024); f.flush(); os.fsync(f.fileno())
    time.sleep(60)
with open(out, "xb") as f:
    chunk = bytes(range(256)) * 4096          # 1 MiB, deterministic
    left = size
    while left > 0:
        n = min(len(chunk), left); f.write(chunk[:n]); left -= n
        if mode == "slow": time.sleep(0.02)
sys.exit(0)
'''


def write_fake_tool(dirpath, name):
    p = Path(dirpath) / name
    p.write_text(FAKE_AVML.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


class DetectionTests(unittest.TestCase):
    def detect(self, system, machine, which=lambda name: None):
        with mock.patch.object(pmb.platform, 'system', return_value=system), \
             mock.patch.object(pmb.platform, 'machine', return_value=machine), \
             mock.patch.object(pmb.shutil, 'which', side_effect=which):
            return {b['name']: b for b in pmb.detect_backends()}

    def test_windows_x64(self):
        with tempfile.TemporaryDirectory() as d:
            exe = write_fake_tool(d, pmb.WINPMEM_BINARY)
            b = self.detect('Windows', 'AMD64', which=lambda n: str(exe) if n == pmb.WINPMEM_BINARY else None)
        self.assertTrue(b['WinPmem']['ready'])
        self.assertFalse(b['AVML']['compatible'])
        self.assertFalse(b['OSXPmem']['compatible'])

    def test_linux_x64(self):
        with tempfile.TemporaryDirectory() as d:
            exe = write_fake_tool(d, 'avml')
            b = self.detect('Linux', 'x86_64', which=lambda n: str(exe) if n == 'avml' else None)
        self.assertTrue(b['AVML']['ready'])
        self.assertFalse(b['WinPmem']['compatible'])

    def test_macos_intel_disabled_without_verified_backend(self):
        b = self.detect('Darwin', 'x86_64', which=lambda n: '/usr/local/bin/osxpmem' if n == 'osxpmem' else None)
        self.assertFalse(b['OSXPmem']['ready'])
        self.assertIn('unsupported', b['OSXPmem']['reason'].lower())

    def test_apple_silicon_has_no_path(self):
        b = self.detect('Darwin', 'arm64', which=lambda n: '/usr/local/bin/osxpmem')
        self.assertFalse(b['OSXPmem']['ready'])
        self.assertIn('Apple Silicon', b['OSXPmem']['reason'])

    def test_not_installed_is_compatible_but_not_ready(self):
        b = self.detect('Linux', 'x86_64')
        self.assertTrue(b['AVML']['compatible'])
        self.assertFalse(b['AVML']['ready'])
        self.assertIn('Not installed', b['AVML']['reason'])


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tool_dir = self.root / 'bin'
        self.tool_dir.mkdir()
        self.tool = write_fake_tool(self.tool_dir, 'avml')
        self.out_dir = self.root / 'evidence'
        self.out_dir.mkdir()
        self.env = mock.patch.dict(os.environ, {'PATH': str(self.tool_dir) + os.pathsep + os.environ['PATH']})
        self.env.start()
        # Pretend we are a Linux x86_64 host running as root with 8 GiB RAM.
        self.patches = [
            mock.patch.object(pmb.platform, 'system', return_value='Linux'),
            mock.patch.object(pmb.platform, 'machine', return_value='x86_64'),
            mock.patch.object(pmb, 'is_elevated', return_value=True),
            mock.patch.object(pmb.psutil, 'virtual_memory', return_value=mock.Mock(total=8 * 1024 ** 3)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.tmp.cleanup()

    def capture(self, name='mem.lime', **kw):
        return pmb.capture_memory('AVML', str(self.tool), str(self.out_dir / name), **kw)

    def test_lime_extension_accepted_and_success_hashed(self):
        with mock.patch.dict(os.environ, {'FAKE_SIZE': str(4 * 1024 * 1024)}):
            r = self.capture()
        data = (self.out_dir / 'mem.lime').read_bytes()
        self.assertEqual(r['size'], len(data))
        self.assertEqual(r['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(r['md5'], hashlib.md5(data).hexdigest())
        self.assertEqual(r['tool'], 'AVML')
        self.assertTrue(Path(r['log']).is_file())
        # Tool is reported as small versus installed RAM (4 MiB of 8 GiB): warning expected.
        self.assertTrue(any('incomplete' in w for w in r['warnings']))

    def test_no_warning_when_size_is_plausible(self):
        with mock.patch.object(pmb.psutil, 'virtual_memory', return_value=mock.Mock(total=4 * 1024 ** 2)), \
             mock.patch.dict(os.environ, {'FAKE_SIZE': str(4 * 1024 * 1024)}):
            r = self.capture()
        self.assertFalse(any('incomplete' in w for w in r['warnings']))

    def test_refuses_without_elevation_before_launching(self):
        with mock.patch.object(pmb, 'is_elevated', return_value=False):
            with self.assertRaises(PermissionError):
                self.capture()
        self.assertEqual(list(self.out_dir.iterdir()), [])   # no image, no log

    def test_rejects_unknown_suffix(self):
        with self.assertRaises(ValueError):
            self.capture('mem.zip')

    def test_refuses_to_overwrite_existing_evidence(self):
        (self.out_dir / 'mem.lime').write_bytes(b'precious')
        with self.assertRaises(FileExistsError):
            self.capture()
        self.assertEqual((self.out_dir / 'mem.lime').read_bytes(), b'precious')

    def test_failure_keeps_partial_and_log(self):
        with mock.patch.dict(os.environ, {'FAKE_MODE': 'fail'}):
            with self.assertRaises(RuntimeError) as ctx:
                self.capture()
        self.assertIn('exited with code 3', str(ctx.exception))
        self.assertTrue((self.out_dir / 'mem.lime').is_file())
        log = (self.out_dir / 'mem.lime.acquisition.log').read_text()
        self.assertIn('boom', log)

    def test_zero_byte_success_is_rejected(self):
        with mock.patch.dict(os.environ, {'FAKE_MODE': 'empty'}):
            with self.assertRaises(RuntimeError):
                self.capture()

    def test_cancel_stops_process_and_keeps_partial(self):
        stop = threading.Event()
        threading.Timer(0.8, stop.set).start()
        started = time.monotonic()
        with mock.patch.dict(os.environ, {'FAKE_MODE': 'hang'}):
            with self.assertRaises(InterruptedError):
                self.capture(cancelled=stop.is_set)
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue((self.out_dir / 'mem.lime').is_file())

    def test_progress_reports_bytes_and_percent(self):
        messages = []
        with mock.patch.dict(os.environ, {'FAKE_SIZE': str(64 * 1024 * 1024), 'FAKE_MODE': 'slow'}):
            self.capture(progress=messages.append)
        self.assertTrue(any('Writing image' in m and 'completion unknown' in m for m in messages), messages[:5])
        self.assertTrue(any(m.startswith('Hashing') or 'Hashing' in m for m in messages))

    def test_unlisted_executable_is_not_trusted(self):
        other = write_fake_tool(self.root, 'avml')   # same name, different path
        with self.assertRaises(pmb.UnsupportedCapture):
            pmb.capture_memory('AVML', str(other), str(self.out_dir / 'x.lime'))


class HashTests(unittest.TestCase):
    def test_parallel_hash_matches_hashlib(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'img.raw'
            data = os.urandom(40 * 1024 * 1024 + 123)     # spans several windows, odd tail
            p.write_bytes(data)
            r = pmb.hash_image(p)
            self.assertEqual(r['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(r['md5'], hashlib.md5(data).hexdigest())

    def test_concurrent_branch_matches_sequential(self):
        # This host has one CPU, so force the two-thread path to prove it is also exact.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'img.lime'
            data = os.urandom(9 * 1024 * 1024 + 7)
            p.write_bytes(data)
            with mock.patch.object(pmb.os, 'cpu_count', return_value=4):
                r = pmb.hash_image(p)
            self.assertEqual(r['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(r['md5'], hashlib.md5(data).hexdigest())

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'e.raw'; p.write_bytes(b'')
            self.assertEqual(pmb.hash_image(p)['sha256'], hashlib.sha256(b'').hexdigest())

    def test_hash_cancel(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'c.raw'; p.write_bytes(os.urandom(4 * 1024 * 1024))
            with self.assertRaises(InterruptedError):
                pmb.hash_image(p, cancelled=lambda: True)


if __name__ == '__main__':
    unittest.main()
