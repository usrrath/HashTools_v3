import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from services.office_conversion_service import convert_office, locate_soffice


class FakeProc:
    returncode = 0
    pid = 1

    def __init__(self, on_start):
        self._on_start = on_start

    def communicate(self, timeout=None):
        self._on_start()
        return '', ''


class OfficeConversionTests(unittest.TestCase):
    def test_invalid_format(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                convert_office(Path(d) / 'missing.docx', Path(d) / 'out', 'exe', '.exe')

    def test_invalid_override(self):
        with patch.dict(os.environ, {'HASHTOOL_SOFFICE': '/does/not/exist/soffice'}):
            with self.assertRaises(RuntimeError):
                locate_soffice()

    def test_isolated_copy_and_output(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / 'original.docx'
            src.write_bytes(b'original evidence')
            executable = root / 'fake_soffice'
            executable.write_text('#!/bin/sh\nexit 0\n')
            executable.chmod(0o700)

            def fake_popen(command, **kwargs):
                copied = Path(command[-1])
                self.assertNotEqual(copied, src)
                self.assertEqual(copied.read_bytes(), src.read_bytes())
                return FakeProc(lambda: (root / 'work' / 'output' / 'office_input.pdf').write_bytes(b'%PDF-test'))

            with patch.dict(os.environ, {'HASHTOOL_SOFFICE': str(executable)}), patch('subprocess.Popen', side_effect=fake_popen):
                output = convert_office(src, root / 'work', 'pdf', '.pdf')
            self.assertEqual(output.read_bytes(), b'%PDF-test')
            self.assertEqual(src.read_bytes(), b'original evidence')

    @unittest.skipIf(os.name == 'nt', 'POSIX process-group test')
    def test_timeout_kills_grandchild(self):
        """A launcher that spawns a long-running child must not leave it behind."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / 'a.docx'
            src.write_bytes(b'x')
            pidfile = root / 'child.pid'
            fake = root / 'fake_soffice'
            fake.write_text(f'#!/bin/sh\nsleep 300 &\necho $! > "{pidfile}"\nwait\n')
            fake.chmod(0o700)
            with patch.dict(os.environ, {'HASHTOOL_SOFFICE': str(fake)}):
                with self.assertRaises(RuntimeError) as ctx:
                    convert_office(src, root / 'work', 'pdf', '.pdf', timeout=2)
            self.assertIn('timed out', str(ctx.exception))
            child = int(pidfile.read_text().strip())
            time.sleep(0.3)
            try:
                os.kill(child, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            # a killed-but-unreaped child shows as zombie; treat via /proc state when available
            if alive and Path(f'/proc/{child}/stat').exists():
                alive = Path(f'/proc/{child}/stat').read_text().split(')')[-1].split()[0] != 'Z'
            self.assertFalse(alive, 'grandchild process survived the timeout')


if __name__ == '__main__':
    unittest.main()
