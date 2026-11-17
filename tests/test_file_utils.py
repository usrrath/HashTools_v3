from pathlib import Path
import os
import tempfile
import unittest

from utils.file_utils import collect_files


class CollectFilesTests(unittest.TestCase):
    def test_parent_named_git_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            case = Path(d) / ".git" / "case"
            case.mkdir(parents=True)
            (case / "evidence.txt").write_text("x")
            self.assertEqual(len(collect_files([str(case)])), 1)

    def test_ignored_names_below_root_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "keep.txt").write_text("k")
            (root / ".DS_Store").write_text("x")
            (root / "__MACOSX").mkdir()
            (root / "__MACOSX" / "junk.txt").write_text("x")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("x")
            found = [Path(p).name for p in collect_files([str(root)])]
            self.assertEqual(found, ["keep.txt"])

    def test_dedupe_and_order(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "b.txt").write_text("b")
            (root / "A.txt").write_text("a")
            found = collect_files([str(root), str(root / "b.txt")])
            self.assertEqual([Path(p).name for p in found], ["A.txt", "b.txt"])

    @unittest.skipIf(os.name == "nt", "symlink test")
    def test_symlink_loop_terminates(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "f.txt").write_text("x")
            os.symlink(root, root / "loop")
            self.assertEqual(len(collect_files([str(root)])), 1)


if __name__ == "__main__":
    unittest.main()
