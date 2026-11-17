"""Tests for the Everything-style search engine (services/search_index.py); no Qt needed."""
import os
import random
import tempfile
import time
import unittest
from pathlib import Path

from services import search_index as si
from services.search_index import FileIndex, QueryError, compile_search, parse_query


def make_tree(root: Path):
    """A tree with awkward names. Returns nothing; the index/reference both walk it."""
    names = {
        "cases/2024/report.pdf": b"x" * 10,
        "cases/2024/Report_FINAL.PDF": b"x" * 2000,
        "cases/2024/notes.txt": b"",
        "cases/2025/summary report.docx": b"x" * 5000,
        "cases/pdf": b"",                      # a file literally named "pdf" (NOT an extension)
        "cases/.pdf": b"",                     # dotfile named like an extension
        "cases/archive.tar.gz": b"x" * 100,
        "cases/folder.pdf/inside.txt": b"y",   # a FOLDER whose name ends in .pdf
        "media/Straße.TXT": b"z",
        "media/İstanbul.pdf": b"z",
        "media/100%_done_under.txt": b"z",
        "media/100AB_done_under.txt": b"z",
        "media/a b c.txt": b"z",
        "media/IMG_0001.jpg": b"z" * 3000,
        "media/IMG_0002.JPG": b"z",
        "media/noext": b"z",
        "media/trailingdot.": b"z",
        ".hidden/secret/invoice.pdf": b"z",
        ".dotfile": b"z",
        "empty_dir/.keep": b"",
        "deep/a/b/c/d/e/needle.pdf": b"n" * 42,
        "reports/q1.pdf": b"z",
        "reports/q1_report.pdf": b"z",
    }
    for rel, data in names.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (root / "empty_dir2").mkdir()
    (root / "link_to_cases").symlink_to(root / "cases", target_is_directory=True)
    try:
        (root / "weird\nname.txt").write_bytes(b"w")  # newline in a file name (POSIX)
    except OSError:
        pass


def reference_entries(root: str):
    """Brute-force listing in the same spirit as the index (no symlink following)."""
    out = []
    stack = [(root, False)]
    while stack:
        path, parent_hidden = stack.pop(0)
        with os.scandir(path) as it:
            for entry in it:
                is_dir = entry.is_dir(follow_symlinks=False)
                hidden = parent_hidden or entry.name.startswith(".")
                out.append((entry.path, entry.name, is_dir, hidden))
                if is_dir:
                    stack.append((entry.path, hidden))
    return out


class SearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name).resolve()
        make_tree(cls.root)
        cls.index = FileIndex(cls.root)
        cls.index.build()
        cls.entries = reference_entries(str(cls.root))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def paths(self, query, hidden=False, index=None, name_filter=""):
        res = (index or self.index).search(query, include_hidden=hidden, name_filter=name_filter)
        return {r[0] for r in res.rows(0, 100000)}

    def rel(self, query, **kw):
        return {os.path.relpath(p, self.root) for p in self.paths(query, **kw)}

    # ---- semantics ------------------------------------------------------------
    def test_counts(self):
        self.assertEqual(self.index.total, len(self.entries))
        self.assertTrue(self.index.complete)

    def test_ext_is_a_real_extension(self):
        found = self.rel("ext:pdf", hidden=True)
        self.assertIn("cases/2024/report.pdf", found)
        self.assertIn("cases/2024/Report_FINAL.PDF", found)      # case-insensitive
        self.assertNotIn("cases/pdf", found)                      # no extension at all (was a bug)
        self.assertNotIn("cases/.pdf", found)                     # dotfile, not an extension
        self.assertNotIn("cases/folder.pdf", found)               # folders are never ext: matches
        self.assertEqual(self.rel("ext:pdf;docx"), self.rel("ext:pdf") | {"cases/2025/summary report.docx"})

    def test_ext_empty_means_no_extension(self):
        found = self.rel("ext: file:")
        self.assertIn("cases/pdf", found)
        self.assertIn("media/noext", found)
        self.assertNotIn("media/IMG_0001.jpg", found)

    def test_glob_matches_whole_name(self):
        found = self.rel("*.pdf", hidden=True)
        self.assertIn("cases/.pdf", found)                        # glob sees the dotfile
        self.assertIn("cases/folder.pdf", found)                  # and folders
        self.assertIn("media/İstanbul.pdf", found)
        self.assertNotIn("cases/pdf", found)
        self.assertEqual(self.rel("q?.pdf"), {"reports/q1.pdf"})
        self.assertEqual(self.rel("q1.p?"), set())  # a wildcard pattern must match the WHOLE name
        self.assertEqual(self.rel("q1.p?f"), {"reports/q1.pdf"})
        self.assertEqual(self.rel("q1*pdf"), {"reports/q1.pdf", "reports/q1_report.pdf"})

    def test_plain_term_matches_name_not_folder(self):
        # "reports" is a folder; with name matching only the files inside do NOT match.
        found = self.rel("reports")
        self.assertEqual(found, {"reports"})
        self.assertIn("reports/q1.pdf", self.rel("path:reports"))
        self.assertIn("reports/q1.pdf", self.rel("reports/"))      # "/" => path match

    def test_and_or_not_phrase(self):
        self.assertEqual(self.rel("summary report"), {"cases/2025/summary report.docx"})
        self.assertEqual(self.rel('"a b c"'), {"media/a b c.txt"})
        self.assertEqual(self.rel("q1 !report"), {"reports/q1.pdf"})
        self.assertEqual(self.rel("needle | noext"), {"deep/a/b/c/d/e/needle.pdf", "media/noext"})
        self.assertEqual(self.rel("ext:jpg | ext:txt report"), self.rel("ext:jpg") | (self.rel("ext:txt") & self.rel("report")))

    def test_unicode_casefold(self):
        self.assertIn("media/Straße.TXT", self.rel("STRASSE"))
        self.assertIn("media/Straße.TXT", self.rel("straße"))

    def test_hidden_toggle_is_instant_and_inherits(self):
        self.assertNotIn(".hidden/secret/invoice.pdf", self.rel("invoice"))
        self.assertIn(".hidden/secret/invoice.pdf", self.rel("invoice", hidden=True))
        self.assertNotIn("empty_dir/.keep", self.rel("keep"))
        self.assertIn("empty_dir/.keep", self.rel("keep", hidden=True))

    def test_parent_path_file_folder(self):
        self.assertEqual(self.rel("parent:2025"), {"cases/2025/summary report.docx"})
        self.assertEqual(self.rel("parent:cases/2024 file:"), {"cases/2024/report.pdf", "cases/2024/Report_FINAL.PDF", "cases/2024/notes.txt"})
        self.assertIn("deep/a/b/c/d/e/needle.pdf", self.rel("path:b/c/d needle"))
        self.assertEqual(self.rel("folder: 2024"), {"cases/2024"})
        self.assertEqual(self.rel("file: folder:"), set())

    def test_size_and_date(self):
        self.assertEqual(self.rel("size:>1kb file:"), {"cases/2024/Report_FINAL.PDF", "cases/2025/summary report.docx", "media/IMG_0001.jpg"})
        self.assertEqual(self.rel("size:=0 ext:txt"), {"cases/2024/notes.txt"})
        self.assertEqual(self.rel("size:40..50 ext:pdf"), {"deep/a/b/c/d/e/needle.pdf"})
        today = time.strftime("%Y-%m-%d")
        self.assertIn("reports/q1.pdf", self.rel(f"dm:{today} q1"))
        self.assertEqual(self.rel("dm:<2000-01-01"), set())

    def test_regex(self):
        self.assertEqual(self.rel(r"regex:^img_\d+\.jpg$"), {"media/IMG_0001.jpg", "media/IMG_0002.JPG"})
        with self.assertRaises(QueryError):
            parse_query("regex:(")
        with self.assertRaises(QueryError):
            parse_query("size:big")

    def test_name_filter_treats_percent_and_underscore_literally(self):
        found = {os.path.basename(p) for p in self.paths("ext:txt", name_filter="100%_done")}
        self.assertEqual(found, {"100%_done_under.txt"})

    def test_newline_in_name_does_not_corrupt_index(self):
        if not (self.root / "weird\nname.txt").exists():
            self.skipTest("file system rejects newlines")
        self.assertIn("weird\nname.txt", {os.path.basename(p) for p in self.paths("weird")})
        self.assertIn("weird\nname.txt", {os.path.basename(p) for p in self.paths("*.txt")})

    # ---- engine == brute force ------------------------------------------------
    def check_against_reference(self, index, queries, entries):
        for query in queries:
            matcher = compile_search(query)
            for hidden in (False, True):
                expected = {p for p, n, d, h in entries if (hidden or not h) and matcher(p, n, d)}
                got = {r[0] for r in index.search(query, include_hidden=hidden).rows(0, 10 ** 6)}
                self.assertEqual(got, expected, f"query={query!r} hidden={hidden}")

    def random_queries(self, n, seed):
        rnd = random.Random(seed)
        words = ["report", "pdf", "q1", "needle", "100", "txt", "a", "e", "img", "straße", "folder", "case", "dot", "."]
        atoms = lambda: rnd.choice([
            lambda: rnd.choice(words), lambda: "*." + rnd.choice(["pdf", "txt", "jpg", "gz", "tar.gz"]),
            lambda: rnd.choice(["q?", "*report*", "i*0?.jpg", "*", "?", "q1*", "img*", "*_under.txt", "*1.pdf", "**pdf", "re*", "*.", "ext*", "*e"]), lambda: "ext:" + rnd.choice(["pdf", "txt;jpg", "gz", "", "docx"]),
            lambda: "parent:" + rnd.choice(["cases", "2024", "cases/2024", "e", "media", "nothing"]),
            lambda: "path:" + rnd.choice(["cases/", "/e/n", "reports", "d/e", "/", "hidden/secret", "2024/rep"]),
            lambda: rnd.choice(["file:", "folder:"]), lambda: "regex:" + rnd.choice(["^q\\d", "pdf$", "a.*b", "^$"]),
            lambda: "size:" + rnd.choice([">1kb", "=0", "<100", "10..3000"]), lambda: rnd.choice(["cases/2024", "reports/q", "/needle", "e/needle"]),
        ])()
        out = []
        for _ in range(n):
            clauses = []
            for _ in range(rnd.choice([1, 1, 1, 2, 3])):
                terms = [("!" if rnd.random() < .18 else "") + atoms() for _ in range(rnd.choice([1, 1, 2, 3]))]
                clauses.append(" ".join(terms))
            out.append(" | ".join(clauses))
        return out

    def test_random_queries_match_brute_force(self):
        self.check_against_reference(self.index, self.random_queries(1500, 1), self.entries)

    def test_many_tiny_segments_and_merges_match_brute_force(self):
        old = (si._MERGE_WHEN, si._MERGE_COUNT)
        si._MERGE_WHEN, si._MERGE_COUNT = 6, 4
        try:
            small = FileIndex(self.root)
            small.flush_min, small.flush_max = 2, 5
            small.build()
            self.assertEqual(small.total, len(self.entries))
            self.assertLessEqual(len(small.segments), 8)          # merging happened
            self.assertGreater(len(small.segments), 1)
            self.check_against_reference(small, self.random_queries(300, 2), self.entries)
        finally:
            si._MERGE_WHEN, si._MERGE_COUNT = old

    def test_paging_and_order_are_stable(self):
        res = self.index.search("file:", include_hidden=True)
        everything = res.rows(0, 10 ** 6)
        self.assertEqual(len(everything), res.total)
        paged = []
        for offset in range(0, res.total, 7):
            paged += res.rows(offset, 7)
        self.assertEqual(paged, everything)

    def test_cancel_stops_build_and_keeps_partial_results(self):
        index = FileIndex(self.root)
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 3
        index.build(cancel=cancel)
        self.assertTrue(index.cancelled and not index.complete)
        self.assertGreater(index.total, 0)
        self.assertLess(index.total, len(self.entries))

    def test_query_while_building_sees_published_segments(self):
        index = FileIndex(self.root)
        index.flush_min = index.flush_max = 3
        seen = []
        index.build(progress=lambda total, done: seen.append((total, done, index.search("*").total)))
        self.assertTrue(seen[-1][1])
        self.assertTrue(any(not done and found > 0 for total, done, found in seen))
        self.assertEqual(index.search("*", include_hidden=True).total, len(self.entries))

    def test_search_can_be_superseded(self):
        with self.assertRaises(InterruptedError):
            self.index.search("report", cancel=lambda: True)

    def test_unreadable_root_is_not_fatal(self):
        index = FileIndex(self.root / "does-not-exist")
        index.build()
        self.assertEqual(index.total, 0)
        self.assertEqual(index.search("x").total, 0)


if __name__ == "__main__":
    unittest.main()
