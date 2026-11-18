"""Real collection tests, not physical RAM acquisition tests."""
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from services.memory_capture_service import collect_response


class LiveResponseTests(unittest.TestCase):
    def test_zip_manifest_hash_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'response.zip'
            result = collect_response(target, 'CASE-1', 'Examiner')
            self.assertTrue(target.is_file())
            self.assertEqual(result['sha256'], hashlib.sha256(target.read_bytes()).hexdigest())
            with zipfile.ZipFile(target) as archive:
                self.assertIsNone(archive.testzip())
                manifest = json.loads(archive.read('manifest.json'))
                content = archive.read('live_response.json')
                self.assertEqual(manifest['artifacts']['live_response.json']['sha256'], hashlib.sha256(content).hexdigest())
                self.assertEqual(json.loads(content)['source_type'], 'live_response_collection')
            with self.assertRaises(FileExistsError):
                collect_response(target, 'CASE-1', 'Examiner')

    def test_cancel_before_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'response.zip'
            with self.assertRaises(InterruptedError):
                collect_response(target, '', '', cancelled=lambda: True)
            self.assertFalse(target.exists())

    def test_reject_non_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                collect_response(Path(tmp) / 'ram.mem', '', '')


if __name__ == '__main__':
    unittest.main()
