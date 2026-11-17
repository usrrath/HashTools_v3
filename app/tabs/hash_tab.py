from __future__ import annotations

from pathlib import Path
import sqlite3
import os
import time

from PyQt6.QtCore import QThread, pyqtSignal, QAbstractTableModel, QModelIndex, Qt, QPoint
from PyQt6.QtWidgets import (
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QTableView,
    QAbstractItemView,
    QVBoxLayout,
    QPushButton,
    QMenu,
    QCheckBox,
    QWidget,
)

from classes.hash_result import HashResult
from services.hash_service import HashWorker
from utils.file_utils import collect_files, human_size
from utils.ui_utils import make_button


class InteractiveHashTable(QTableView):
    """Dispatch mouse actions from the actual table viewport on macOS/Windows."""
    row_activated = pyqtSignal(int)
    row_menu_requested = pyqtSignal(int, QPoint)

    def mouseDoubleClickEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and index.isValid():
            self.row_activated.emit(index.row())
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        # QAbstractScrollArea forwards viewport context-menu events here.
        point = self.viewport().mapFromGlobal(event.globalPos())
        index = self.indexAt(point)
        if index.isValid():
            self.row_menu_requested.emit(index.row(), event.globalPos())
            event.accept()
            return
        super().contextMenuEvent(event)


class HashResultsModel(QAbstractTableModel):
    """One continuous, scrollable result set; bounded SQLite block cache."""
    HEADERS = ("No.", "File Name", "Size", "MD5", "SHA1", "SHA256", "SHA512", "BLAKE3")
    BLOCK = 256
    MAX_BLOCKS = 12
    # SQLite IDs are 64-bit; 100,000,000 rows fit within Qt's 32-bit model row range.
    MAX_QT_ROWS = 2_147_483_647

    def __init__(self, parent=None):
        super().__init__(parent)
        from collections import OrderedDict
        self._cache = OrderedDict()
        self._db = None
        self._count = 0
        self._live = []
        self._last_refresh = 0.0

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self._count

    def set_algorithms(self, algorithms):
        self.beginResetModel()
        self.algorithms = tuple(algorithms)
        self.endResetModel()

    def _columns(self):
        return ("No.", "File Name", "Size") + tuple(a.upper() for a in getattr(self, "algorithms", ("md5", "sha1", "sha256", "sha512")))

    def columnCount(self, parent=QModelIndex()):
        return len(self._columns())

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self._columns()[section]
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Vertical:
            return str(section + 1)
        return None

    def reset(self):
        self.beginResetModel()
        if self._db is not None:
            self._db.close()
        self._db = None
        self._cache.clear()
        self._live.clear()
        self._count = 0
        self.endResetModel()

    def add_live(self, result):
        # Legacy signal compatibility: SQLite is the single source of truth.
        return

    def open_database(self, path):
        self.beginResetModel()
        if self._db is not None:
            self._db.close()
        self._db = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True, timeout=0.2)
        self._db.execute("PRAGMA query_only=ON")
        self._db.execute("PRAGMA busy_timeout=200")
        self._cache.clear()
        self._live.clear()
        self._count = min(self.MAX_QT_ROWS, self._db.execute("SELECT COALESCE(MAX(id), 0) FROM results").fetchone()[0])
        self._last_refresh = time.monotonic()
        self.endResetModel()

    def refresh_count(self, force=False):
        if self._db is None:
            return
        if not force and time.monotonic() - self._last_refresh < 0.35:
            return
        self._last_refresh = time.monotonic()
        try:
            latest = min(self.MAX_QT_ROWS, self._db.execute("SELECT COALESCE(MAX(id), 0) FROM results").fetchone()[0])
        except sqlite3.Error:
            return
        if latest < self._count:
            self.beginResetModel()
            self._cache.clear()
            self._count = latest
            self.endResetModel()
        elif latest > self._count:
            # Invalidate the previous last block (it may have been incomplete).
            self._cache.pop(self._count // self.BLOCK, None)
            if self._count:
                self._cache.pop((self._count - 1) // self.BLOCK, None)
            self.beginInsertRows(QModelIndex(), self._count, latest - 1)
            self._count = latest
            self.endInsertRows()

    def _get_row(self, index):
        if self._db is None:
            return self._live[index] if index < len(self._live) else None
        block = index // self.BLOCK
        if block not in self._cache:
            # Results are written with monotonically increasing IDs starting at 1.
            # Indexed ID range avoids increasingly expensive OFFSET scans.
            first_id = block * self.BLOCK + 1
            rows = self._db.execute(
                "SELECT path,size,md5,sha1,sha256,sha512,blake3,success,error,warning "
                "FROM results WHERE id >= ? ORDER BY id LIMIT ?",
                (first_id, self.BLOCK)).fetchall()
            self._cache[block] = rows
            if len(self._cache) > self.MAX_BLOCKS:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(block)
        rows = self._cache[block]
        offset = index % self.BLOCK
        return rows[offset] if offset < len(rows) else None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        row = self._get_row(index.row())
        if row is None:
            return None
        path, size, md5, sha1, sha256, sha512, blake3, success, error, warning = row
        values = (f"{index.row() + 1:,}", os.path.basename(path) + (" ⚠" if warning else ""),
                  human_size(size or 0) if success else "Error",
                  md5 if success else error, sha1, sha256, sha512, blake3)
        if role == Qt.ItemDataRole.ToolTipRole and index.column() == 1:
            return f"{path}\n{warning}" if warning else path
        columns = (0, 1, 2) + tuple(self.HEADERS.index(a.upper()) for a in getattr(self, "algorithms", ("md5", "sha1", "sha256", "sha512")))
        return str(values[columns[index.column()]] or "")


class HashTab(QWidget):
    progress_changed = pyqtSignal(int, str)
    new_selection_requested = pyqtSignal(list)
    hashing_finished = pyqtSignal()
    open_result_requested = pyqtSignal(str, str)  # destination, original file path

    def __init__(self):
        super().__init__()
        self.results: list[HashResult] = []
        self._paths: set[str] = set()
        self._thread: QThread | None = None
        self._worker: HashWorker | None = None
        self._hashing = False
        self._completed_count = 0
        self._cancelled = False
        self.result_db_path = None
        self._total_stored = 0
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.add_files_btn = make_button("📁 Add Files")
        self.add_files_btn.clicked.connect(self.choose_files)

        self.add_folder_btn = make_button("📂 Add Folder")
        self.add_folder_btn.clicked.connect(self.choose_folder)

        self.pause_btn = make_button("Pause")
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.pause_btn.setEnabled(False)
        self._paused = False

        self.cancel_btn = make_button("Stop")
        self.cancel_btn.clicked.connect(self.cancel_hashing)
        self.cancel_btn.setEnabled(False)

        self.clear_btn = make_button("🗑 Clear")
        self.clear_btn.clicked.connect(self.clear)

        self.status = QLabel("No files loaded")
        self.eta_label = QLabel("Elapsed: 0s | Remaining: --")
        self.transfer_label = QLabel("0 B / 0 B  |  0 B/s  |  Waiting...")
        self.transfer_label.setToolTip("Live throughput and byte progress (similar to pv)")
        self.progress = QProgressBar()
        self.progress.setMinimumWidth(260)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("0%")

        bar.addWidget(self.add_files_btn)
        bar.addWidget(self.add_folder_btn)
        bar.addWidget(self.clear_btn)
        bar.addWidget(self.cancel_btn)
        bar.addWidget(self.pause_btn)
        bar.addStretch(1)
        root.addLayout(bar)

        algorithm_row = QHBoxLayout()
        algorithm_row.addWidget(QLabel("Hash algorithms:"))
        self.algorithm_checks = {}
        for name in ("md5", "sha1", "sha256", "sha512"):
            checkbox = QCheckBox(name.upper())
            checkbox.setChecked(True)
            checkbox.toggled.connect(self._sync_all_checkbox)
            self.algorithm_checks[name] = checkbox
            algorithm_row.addWidget(checkbox)
        self.all_checkbox = QCheckBox("ALL")
        self.all_checkbox.setChecked(True)
        self.all_checkbox.toggled.connect(self._toggle_all_algorithms)
        algorithm_row.addWidget(self.all_checkbox)
        algorithm_row.addStretch()
        root.addLayout(algorithm_row)

        progress_row = QHBoxLayout()
        progress_row.addWidget(self.status, 3)
        progress_row.addWidget(self.eta_label, 2)
        progress_row.addWidget(self.progress, 2)
        root.addLayout(progress_row)
        root.addWidget(self.transfer_label)

        self.model = HashResultsModel(self)
        self.table = InteractiveHashTable(self)
        self.table.setModel(self.model)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 75)
        self.table.setColumnWidth(1, 300)
        self.table.setColumnWidth(2, 110)
        self.table.verticalHeader().setDefaultSectionSize(25)
        self.table.verticalHeader().hide()  # Use the dedicated No. column, not duplicate row labels
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.table.viewport().setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.table.row_activated.connect(lambda row: self._open_result(row, "metadata"))
        self.table.row_menu_requested.connect(self._show_result_menu)
        self.table.setToolTip("Double-click: Metadata | Right-click: Metadata or Hex View")
        root.addWidget(self.table)
        actions = QHBoxLayout()
        self.open_metadata_btn = QPushButton("Open selected in Metadata")
        self.open_hex_btn = QPushButton("Open selected in Hex View")
        self.open_metadata_btn.clicked.connect(lambda: self._open_selected("metadata"))
        self.open_hex_btn.clicked.connect(lambda: self._open_selected("hex"))
        actions.addWidget(self.open_metadata_btn)
        actions.addWidget(self.open_hex_btn)
        actions.addStretch()
        root.addLayout(actions)
        self.count_label = QLabel("0 results · Continuous scrolling · No pagination")
        root.addWidget(self.count_label)

    def selected_algorithms(self):
        return tuple(name for name, checkbox in self.algorithm_checks.items() if checkbox.isChecked())

    def _sync_all_checkbox(self):
        self.all_checkbox.blockSignals(True)
        self.all_checkbox.setChecked(all(c.isChecked() for c in self.algorithm_checks.values()))
        self.all_checkbox.blockSignals(False)
        if not self._hashing and self.model.rowCount() == 0:
            self.model.set_algorithms(self.selected_algorithms())

    def _toggle_all_algorithms(self, checked):
        for checkbox in self.algorithm_checks.values():
            checkbox.blockSignals(True)
            checkbox.setChecked(checked)
            checkbox.blockSignals(False)
        if not self._hashing and self.model.rowCount() == 0:
            self.model.set_algorithms(self.selected_algorithms())

    def _open_selected(self, destination: str) -> None:
        index = self.table.currentIndex()
        if not index.isValid():
            QMessageBox.information(self, "Select a file", "Select a hashed file row first.")
            return
        self._open_result(index.row(), destination)

    def _selected_result_path(self, row: int) -> str | None:
        """Resolve the clicked row, not the selected row or its visible filename."""
        if not (0 <= row < self.model.rowCount()):
            return None
        try:
            record = self.model._get_row(row)
        except (sqlite3.Error, IndexError, OSError) as exc:
            QMessageBox.warning(self, "Result lookup failed", str(exc))
            return None
        return os.fspath(record[0]) if record and record[0] else None

    def _open_result(self, row: int, destination: str) -> None:
        path = self._selected_result_path(row)
        if not path:
            QMessageBox.warning(self, "Result unavailable", "No stored path was found for this row.")
            return
        if not Path(path).is_file():
            QMessageBox.warning(self, "File unavailable", f"The original file is no longer accessible:\n{path}")
            return
        # The main window installs this callback after constructing all tabs.
        # A direct call avoids losing navigation when an older signal connection
        # is absent or connected to a different HashTab instance.
        handler = getattr(self, "navigation_handler", None)
        if callable(handler):
            try:
                handler(destination, path)
            except Exception as exc:
                QMessageBox.critical(self, "Unable to open hashed file", f"{type(exc).__name__}: {exc}")
            return
        # Compatibility with applications using only the Qt signal.
        self.open_result_requested.emit(destination, path)

    def _show_result_menu(self, row: int, global_position: QPoint) -> None:
        if not (0 <= row < self.model.rowCount()):
            return
        self.table.selectRow(row)
        menu = QMenu(self.table)
        metadata_action = menu.addAction("Open in Metadata")
        hex_action = menu.addAction("Open in Hex View")
        chosen = menu.exec(global_position)
        if chosen is metadata_action:
            self._open_result(row, "metadata")
        elif chosen is hex_action:
            self._open_result(row, "hex")

    def choose_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Select files", str(Path.home()), "All Files (*.*)")
        if files:
            self.new_selection_requested.emit(files)

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select folder", str(Path.home()))
        if folder:
            self.new_selection_requested.emit([folder])

    @property
    def is_hashing(self) -> bool:
        return self._hashing

    def load_file(self, path: str) -> None:
        self.load_files([path])

    def load_files(self, paths: list[str], scanned: bool = False) -> None:
        """Replace the previous analysis results and hash only the new selection."""
        if self._hashing:
            QMessageBox.information(self, "Hashing in progress", "Please wait until the current hashing job is complete.")
            return
        files = list(paths)
        self.clear()
        if files:
            self._start_hashing(files)

    def add_paths(self, paths: list[str]) -> None:
        if self._hashing:
            QMessageBox.information(self, "Hashing in progress", "Please wait until the current hashing job is complete.")
            return

        files = list(paths)
        if not files:
            return

        new_files: list[str] = []
        for file_path in files:
            try:
                key = str(Path(file_path).resolve())
            except Exception:
                key = str(file_path)
            if key not in self._paths:
                new_files.append(file_path)

        if not new_files:
            self.status.setText(f"{len(self.results)} file(s) loaded")
            return

        self._start_hashing(new_files)

    def _start_hashing(self, files: list[str]) -> None:
        algorithms = self.selected_algorithms()
        if not algorithms:
            QMessageBox.warning(self, "Select an algorithm", "Check at least one hash algorithm before calculating.")
            return
        self._job_algorithms = algorithms
        self.model.set_algorithms(algorithms)
        for checkbox in (*self.algorithm_checks.values(), self.all_checkbox):
            checkbox.setEnabled(False)
        self._hashing = True
        self._cancelled = False
        self._paused = False
        self.pause_btn.setText("Pause")
        self.pause_btn.setEnabled(True)
        self._completed_count = 0
        self._total_stored = 0
        self.result_db_path = None
        self.model.reset()
        self.count_label.setText("Hashing... results appear live as they are committed to SQLite")
        self.cancel_btn.setEnabled(True)
        self._set_buttons_enabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("0%")
        self.status.setText(f"Hashing 0/{len(files)} file(s)...")
        self.eta_label.setText("Elapsed: 0s | Remaining: Counting files...")
        self.transfer_label.setText("Indexing files and calculating total bytes...")
        self.progress_changed.emit(0, "Hashing files... 0%")

        thread = QThread(self)
        worker = HashWorker(files, algorithms=algorithms)
        self._thread, self._worker = thread, worker
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.database_ready.connect(self._on_database_ready)
        worker.results_batch.connect(self._on_results_batch)
        worker.file_completed.connect(self._on_file_completed)
        worker.progress.connect(self._on_worker_progress)
        worker.timing.connect(self._on_worker_timing)
        worker.transfer.connect(self._on_worker_transfer)
        worker.error.connect(self._on_worker_error)
        worker.finished.connect(self._on_worker_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        # Bind this job's own thread/worker: a late signal from an OLD job must never
        # clear the references of a NEWER job (that made shutdown unable to cancel it).
        thread.finished.connect(lambda t=thread: self._cleanup_worker_refs(t))

        thread.start()

    def _on_database_ready(self, path: str) -> None:
        self.result_db_path = path
        try:
            self.model.open_database(path)
        except sqlite3.Error as exc:
            self.status.setText(f"Unable to open live results: {exc}")

    def _on_results_batch(self, results: list) -> None:
        # Do not allocate Qt widgets or Python objects for every file.
        self.model.refresh_count(force=True)
        self.count_label.setText(f"{self.model.rowCount():,} stored results · Live continuous scroll · No pagination")

    def _on_file_completed(self, result: HashResult) -> None:
        # Preserve existing report compatibility with a bounded sample.
        if len(self.results) < 5000:
            self.results.append(result)

    def _on_worker_progress(self, current: int, total: int, percent: int, message: str) -> None:
        if percent < 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(percent)
            self.progress.setFormat(f"{percent}%")
        self.status.setText(message)
        self.model.refresh_count()
        self.count_label.setText(f"{self.model.rowCount():,} stored results · Live continuous scroll · No pagination")
        if total > 0:
            self._completed_count = max(self._completed_count, current)
        self.progress_changed.emit(max(0, percent), message)

    def _on_worker_timing(self, elapsed: str, remaining: str) -> None:
        self.eta_label.setText(f"Elapsed: {elapsed} | Remaining: {remaining}")

    def _on_worker_transfer(self, done: int, total: int, speed: float, path: str) -> None:
        pct = f"{done / total * 100:.1f}%" if total else "unknown"
        self.transfer_label.setText(
            f"{human_size(done)} / {human_size(total)} ({pct})  |  "
            f"{human_size(int(speed))}/s  |  {Path(path).name if path else 'Preparing...'}"
        )
        self.transfer_label.setToolTip(path)

    def _on_worker_error(self, message: str) -> None:
        # Do not show a popup for every failed file in large folders.
        self.status.setText(message)

    def _on_worker_finished(self) -> None:
        self._hashing = False
        self.result_db_path = self._worker.result_db_path if self._worker else None
        if self.result_db_path and os.path.isfile(self.result_db_path):
            try:
                with sqlite3.connect(self.result_db_path, timeout=3) as db:
                    self._total_stored = db.execute("SELECT COUNT(*) FROM results").fetchone()[0]
                self.model.open_database(self.result_db_path)
                self.count_label.setText(f"{self._total_stored:,} results · Scroll continuously · No pagination")
            except sqlite3.Error as exc:
                self.status.setText(f"Cannot load stored results: {exc}")
        self.cancel_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self._set_buttons_enabled(True)
        for checkbox in (*self.algorithm_checks.values(), self.all_checkbox):
            checkbox.setEnabled(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0 if self._cancelled else 100)
        self.progress.setFormat("Cancelled" if self._cancelled else "100%")
        self.status.setText(f"{self._completed_count:,} processed; {self._total_stored:,} stored; scroll all results" + (" (cancelled)" if self._cancelled else ""))
        self.progress_changed.emit(0 if self._cancelled else 100, self.status.text())
        self.hashing_finished.emit()

    def _cleanup_worker_refs(self, finished_thread=None) -> None:
        if finished_thread is None or finished_thread is self._thread:
            self._thread = None
            self._worker = None

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self.add_files_btn.setEnabled(enabled)
        self.add_folder_btn.setEnabled(enabled)
        self.clear_btn.setEnabled(enabled)

    def clear(self) -> None:
        if self._hashing:
            QMessageBox.information(self, "Hashing in progress", "Please wait until hashing is complete before clearing.")
            return

        self.results.clear()
        self._total_stored = 0
        self.result_db_path = None
        self._paths.clear()
        self.model.reset()
        self.count_label.setText("0 results · Continuous scrolling · No pagination")
        self.progress.setValue(0)
        self.progress.setFormat("0%")
        self.status.setText("No files loaded")
        self.eta_label.setText("Elapsed: 0s | Remaining: --")
        self.progress_changed.emit(0, "Cleared")

    def toggle_pause(self) -> None:
        if not self._worker:
            return
        self._paused = not self._paused
        if self._paused:
            self._worker.pause()
            self.pause_btn.setText("Resume")
            self.status.setText("Paused")
            self.eta_label.setText(self.eta_label.text() + " (paused)")
        else:
            self._worker.resume()
            self.pause_btn.setText("Pause")
            self.eta_label.setText(self.eta_label.text().replace(" (paused)", ""))

    def cancel_hashing(self) -> None:
        if self._worker is not None:
            self._cancelled = True
            self._worker.cancel()
            self.pause_btn.setEnabled(False)
            self.cancel_btn.setEnabled(False)
            self.status.setText("Cancelling...")

    def shutdown(self) -> None:
        """Cancel running hashing and wait for the thread (called by the main window)."""
        worker, thread = self._worker, self._thread
        if worker is not None:
            worker.cancel()
        if thread is not None:
            thread.quit()
            thread.wait(10000)

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)
