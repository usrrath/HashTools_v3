"""Headless check of the Evidence Explorer search wiring (skipped when Qt cannot start)."""
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PyQt6.QtWidgets import QApplication
    import app.tabs.evidence_explorer_tab as explorer_module
    from app.tabs.evidence_explorer_tab import EvidenceExplorerTab
    # QtMultimedia crashes in headless sandboxes (also in the original code); the tab supports running without it.
    explorer_module.QMediaPlayer = explorer_module.QAudioOutput = explorer_module.QGraphicsVideoItem = None
    QT_OK = True
except Exception:  # pragma: no cover - Qt/libGL missing
    QT_OK = False


@unittest.skipUnless(QT_OK, "PyQt6 widgets unavailable")
class SearchUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name).resolve()
        for i in range(400):
            d = cls.root / f"dir{i % 20}"
            d.mkdir(exist_ok=True)
            (d / f"report_{i}.pdf").write_bytes(b"x")
            (d / f"photo_{i}.jpg").write_bytes(b"x")
        (cls.root / ".hid").mkdir()
        (cls.root / ".hid" / "report_hidden.pdf").write_bytes(b"x")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.tab = EvidenceExplorerTab()
        self.tab.root_input.setText(str(self.root))
        self.tab.open_root()

    def tearDown(self):
        self.tab.close()
        self.tab.deleteLater()

    def pump(self, until, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            self.app.processEvents()
            if until():
                return True
            time.sleep(0.01)
        return False

    def search(self, text, **kw):
        self.tab.search_input.blockSignals(True)
        self.tab.search_input.setText(text)
        self.tab.search_input.blockSignals(False)
        self.tab.search_everything(**kw)
        self.assertTrue(self.pump(lambda: not self.tab._index_building() and self.tab.page_status.text() not in ("", "Indexing…", "Filtering results…")))
        self.app.processEvents()
        return self.tab

    def test_first_search_indexes_then_next_search_is_instant(self):
        tab = self.search("ext:pdf")
        self.assertTrue(tab.file_index.complete)
        self.assertEqual(tab._visible_search_total, 400)           # hidden folder excluded
        self.assertEqual(tab.files.rowCount(), tab.search_page_size)
        index_before = tab.file_index
        tab.search_input.setText("photo_1*")                        # typed: debounce timer -> instant re-query
        self.assertTrue(self.pump(lambda: tab._visible_search_total == 111 and tab.search_query == "photo_1*"))
        self.assertIs(tab.file_index, index_before)                 # no rescan

    def test_hidden_toggle_and_name_filter(self):
        tab = self.search("report")
        self.assertEqual(tab._visible_search_total, 400)
        tab.show_hidden.setChecked(True)
        self.assertTrue(self.pump(lambda: tab._visible_search_total == 401))
        tab.filter_input.setText("report_hidden")
        self.assertTrue(self.pump(lambda: tab._visible_search_total == 1))

    def test_invalid_query_reports_error_without_crashing(self):
        tab = self.search("ext:pdf")
        tab.search_input.setText('regex:(')
        tab.search_everything()
        self.assertIn("Invalid", tab.search_status.text())

    def test_paging(self):
        tab = self.search("ext:pdf")
        first = tab.files.item(0, 0).text()
        tab.show_search_page(1)
        self.assertTrue(self.pump(lambda: tab.search_page == 1))
        self.assertNotEqual(tab.files.item(0, 0).text(), first)

    def test_reindex_and_clear(self):
        tab = self.search("ext:jpg")
        old = tab.file_index
        tab.search_everything(reindex=True)
        self.assertIsNot(tab.file_index, old)
        self.assertTrue(self.pump(lambda: tab.file_index.complete))
        tab.clear_search()
        self.assertFalse(tab.search_mode)
        self.assertIsNotNone(tab.file_index)

    def test_changing_root_drops_index(self):
        tab = self.search("ext:jpg")
        tab.root_input.setText(str(self.root / "dir1"))
        tab.open_root()
        self.assertIsNone(tab.file_index)


if __name__ == "__main__":
    unittest.main()
