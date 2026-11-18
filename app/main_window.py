from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QMainWindow, QTabWidget, QStatusBar, QProgressBar, QLabel, QMessageBox
from PyQt6.QtGui import QFont, QFontDatabase, QShortcut, QKeySequence

from config.settings import APP_NAME
from app.tabs.home_tab import HomeTab
from app.tabs.hash_tab import HashTab
from app.tabs.metadata_tab import MetadataTab
from app.tabs.hex_tab import HexViewTab
from app.tabs.evidence_explorer_tab import EvidenceExplorerTab
from app.tabs.memory_capture_tab import MemoryCaptureTab
from app.tabs.memory_analysis_tab import MemoryAnalysisTab
from app.tabs.report_tab import ReportTab
from utils.file_utils import collect_files

STYLE = """
QMainWindow, QWidget { background:#f8fafc; color:#111827; font-size:14px; }
QTabWidget::pane { border:1px solid #d1d5db; background:#ffffff; }
QTabBar::tab { background:#f1f5f9; padding:9px 18px; border:1px solid #cbd5e1; border-bottom:0; }
QTabBar::tab:selected { background:#ffffff; color:#2563eb; font-weight:700; border-top:3px solid #2563eb; }
QLineEdit, QComboBox { background:#ffffff; border:1px solid #cbd5e1; border-radius:6px; padding:7px; }
QTableWidget { background:#ffffff; alternate-background-color:#f8fafc; gridline-color:#334155; border:1px solid #cbd5e1; }
QHeaderView::section { background:#e5e7eb; color:#111827; padding:8px; border:1px solid #cbd5e1; font-weight:700; }
QFrame#card, QFrame#toolbar, QFrame#sidePanel { background:#ffffff; border:1px solid #e5e7eb; border-radius:12px; }
QFrame#moduleCard { background:#f8fafc; border:1px solid #dbeafe; border-radius:14px; padding:12px; }
QLabel#khTitle { font-size:24px; color:#1e3a8a; font-weight:700; }
QLabel#enTitle { font-size:18px; color:#111827; font-weight:800; }
QLabel#sectionTitle { font-size:18px; font-weight:800; }
QLabel#moduleIcon { font-size:38px; color:#2563eb; }
QLabel#moduleTitle { font-size:17px; font-weight:800; color:#2563eb; }
QLabel#infoBox, QLabel#summaryBox { background:#ffffff; border:1px solid #e5e7eb; border-radius:10px; padding:12px; }
"""


class HashToolsWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.current_file: str | None = None
        self.current_files: list[str] = []
        self.setWindowTitle(f"{APP_NAME}")
        self.resize(1500, 950)

        self.tabs = QTabWidget()
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setMovable(False)
        self.setCentralWidget(self.tabs)
        self.home_tab = HomeTab()
        self.hash_tab = HashTab()
        self.metadata_tab = MetadataTab()
        self.hex_tab = HexViewTab()
        self.report_tab = ReportTab(self._report_state, lambda: self.hash_tab.result_db_path, lambda: getattr(self.hash_tab, "_job_algorithms", self.hash_tab.selected_algorithms()))
        self.evidence_tab = EvidenceExplorerTab()
        self.memory_capture_tab = MemoryCaptureTab()
        self.memory_analysis_tab = MemoryAnalysisTab()

        self.tabs.addTab(self.home_tab, "🏠 Home")
        self.tabs.addTab(self.evidence_tab, "🔎 Evidence Explorer")
        self.tabs.addTab(self.hash_tab, "📊 Hashing Calculator")
        self.tabs.addTab(self.metadata_tab, "📋 MetaData")
        self.tabs.addTab(self.hex_tab, "🔢 Hex View")
        self.tabs.addTab(self.report_tab, "📄 Report")
        self.tabs.addTab(self.memory_capture_tab, "🧠 Memory Capture")
        self.tabs.addTab(self.memory_analysis_tab, "🔬 Memory Analysis")
        # Open the explorer by default so the new tab is immediately visible.
        self.tabs.setCurrentWidget(self.evidence_tab)
        self.explorer_shortcut = QShortcut(QKeySequence("Ctrl+E"), self)
        self.explorer_shortcut.activated.connect(lambda: self.tabs.setCurrentWidget(self.evidence_tab))

        # Any new upload/selection starts a completely fresh analysis session.
        self.home_tab.files_selected.connect(self.start_new_session)
        self.hash_tab.new_selection_requested.connect(self.start_new_session)
        self.metadata_tab.file_selected.connect(lambda path: self.start_new_session([path]))
        self.hex_tab.file_loaded.connect(lambda path: self.start_new_session([path]))
        self.home_tab.list_widget.itemDoubleClicked.connect(self._home_file_double_clicked)

        self.hash_tab.progress_changed.connect(self.update_progress)
        self.hash_tab.hashing_finished.connect(lambda: self.report_tab.set_ready(True))
        self.hash_tab.navigation_handler = self._open_hashed_result
        self.hash_tab.open_result_requested.connect(self._open_hashed_result)
        self.report_tab.progress_changed.connect(self.update_progress)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status_label = QLabel("System Ready.")
        self.status_progress = QProgressBar()
        self.status_progress.setRange(0, 100)
        self.status_progress.setValue(0)
        self.status_progress.setFormat("0%")
        self.status_progress.setFixedWidth(180)
        self.status.addWidget(self.status_label, 1)
        self.status.addPermanentWidget(self.status_progress)
        self.status.showMessage("System Ready.")

    def closeEvent(self, event) -> None:
        """Qt sends closeEvent only to top-level windows, so shut children down here."""
        self.memory_capture_tab.shutdown()
        self.memory_analysis_tab.shutdown()
        self.hash_tab.shutdown()
        self.evidence_tab.close()  # stops search worker, clears previews, removes temp data
        super().closeEvent(event)

    def start_new_session(self, paths: list[str]) -> None:
        """Clear all old UI/report/hash state and load only the newly selected files."""
        if self.hash_tab.is_hashing:
            QMessageBox.information(
                self,
                "Hashing in progress",
                "Please wait until the current hashing job is complete before loading a new file or folder.",
            )
            return

        files = list(paths)
        if not files:
            return

        self.current_files = files
        self.current_file = files[0]

        # Refresh every tab from the same new batch, preventing stale cross-tab data.
        self.home_tab.set_files([x for x in files if Path(x).is_file()][:100], emit_signals=False, scanned=True)
        self.metadata_tab.set_files([x for x in files if Path(x).is_file()][:100], selected_path=self.current_file, scanned=True)
        self.hex_tab.set_files([x for x in files if Path(x).is_file()][:100], selected_path=self.current_file, scanned=True)
        self.report_tab.reset()
        self.hash_tab.load_files(files, scanned=True)

        self.update_progress(0, "Selected sources; background discovery and hashing started.")
        self.status.showMessage(f"New analysis loaded: {len(files)} file(s). Primary: {self.current_file}")

    def load_file_everywhere(self, path: str) -> None:
        """Backward-compatible entry point: a direct file load starts a new session."""
        self.start_new_session([path])

    def load_files_everywhere(self, paths: list[str]) -> None:
        """Backward-compatible entry point for a new batch."""
        self.start_new_session(paths)

    def _home_file_double_clicked(self, item) -> None:
        """Open a file from the current batch in Metadata and Hex without resetting hashes."""
        row = self.home_tab.list_widget.row(item)
        if 0 <= row < len(self.current_files):
            path = self.current_files[row]
            self.current_file = path
            self.metadata_tab.load_file(path)
            self.hex_tab.load_file(path, emit_signal=False)
            self.status.showMessage(f"Viewing: {Path(path).name}")

    def _open_hashed_result(self, destination: str, path: str) -> None:
        """Inspect a hashed file without clearing or restarting the hashing session."""
        if not Path(path).is_file():
            QMessageBox.warning(self, "File unavailable", f"Cannot open original file:\n{path}")
            return
        self.current_file = path
        if destination == "metadata":
            target = self.metadata_tab
        elif destination == "hex":
            target = self.hex_tab
        else:
            QMessageBox.warning(self, "Invalid destination", destination)
            return
        # Switch first so users see the navigation even if metadata extraction
        # takes time for a large file. Do not reset the hashing session.
        self.tabs.setCurrentWidget(target)
        self.status.showMessage(f"Opening {Path(path).name} in {destination}...")
        QApplication.processEvents()
        if destination == "metadata":
            target.load_file(path)
        else:
            target.load_file(path, emit_signal=False)
        self.status.showMessage(f"Inspecting hashed file: {path}")

    def update_progress(self, value: int, message: str = "") -> None:
        value = max(0, min(100, int(value)))
        self.status_progress.setValue(value)
        self.status_progress.setFormat(f"{value}%")
        self.status_label.setText(message or f"Progress {value}%")

    def _report_state(self):
        # The report receives only the current batch's freshly generated results.
        return self.current_file, self.metadata_tab.rows, self.hash_tab.results


def _apply_safe_font(app: QApplication) -> None:
    """Choose an installed macOS UI font; let Qt fall back for Khmer glyphs."""
    families = {name.casefold(): name for name in QFontDatabase.families()}
    # Avoid Arial and Qt's virtual Sans-serif alias on macOS.
    for name in ("Helvetica Neue", "Helvetica", "Noto Sans", "DejaVu Sans", "Khmer Sangam MN", "Khmer MN"):
        actual = families.get(name.casefold())
        if actual:
            font = QFont(actual, 10)
            font.setStyleStrategy(QFont.StyleStrategy.PreferDefault)
            app.setFont(font)
            return
    # Use the platform-provided font instead of requesting a nonexistent family.
    font = app.font()
    font.setPointSize(10)
    app.setFont(font)


def run_app() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    _apply_safe_font(app)
    app.setStyleSheet(STYLE)
    window = HashToolsWindow()
    window.show()
    sys.exit(app.exec())
