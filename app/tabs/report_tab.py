from __future__ import annotations

import re
import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QObject, QThread, pyqtSlot
from PyQt6 import sip
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QFileDialog, QMessageBox, QProgressBar

from services.pdf_report_service import PDFReportService
from services.large_pdf_export import export_all_hashes
import os
from utils.ui_utils import make_button


class FullPDFWorker(QObject):
    progress = pyqtSignal(int, str)
    completed = pyqtSignal(str, int)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, db_path, output, algorithms, format="pdf"):
        self.algorithms = tuple(algorithms)
        self.format = format
        super().__init__()
        self.db_path, self.output = db_path, output

    @pyqtSlot()
    def run(self):
        try:
            total = export_json_hashes(self.db_path, self.output, self.algorithms, self.progress) if self.format == "json" else export_all_hashes(
                self.db_path, self.output, algorithms=self.algorithms,
                progress=lambda done, count: self.progress.emit(
                    int(done * 100 / count), f"PDF export: {done:,}/{count:,} files"
                ),
            )
            self.completed.emit(self.output, total)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class ReportTab(QWidget):
    progress_changed = pyqtSignal(int, str)

    def __init__(self, get_state, get_hash_database=None, get_algorithms=None):
        super().__init__()
        self.get_state = get_state
        self.get_hash_database = get_hash_database or (lambda: None)
        self.get_algorithms = get_algorithms or (lambda: ("md5", "sha1", "sha256", "sha512"))
        self._thread = None
        self._worker = None
        self._json_exporting = False
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        info = QLabel(
            "Export a clean A4 landscape PDF report. The default PDF file name follows the uploaded file name, "
            "and the report uses a full-width MD5 / SHA1 / SHA256 table."
        )
        info.setWordWrap(True)
        root.addWidget(info)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("0%")
        root.addWidget(self.progress)

        self.export_btn = make_button("📄 Export PDF Report", True)
        self.export_btn.clicked.connect(self.export)
        self.all_btn = make_button("📄 Export ALL Calculated Hashes (PDF)", True)
        self.all_btn.clicked.connect(self.export_all)
        root.addWidget(self.all_btn)
        self.json_btn = make_button("📋 Export Selected Hashes (JSON)", True)
        self.json_btn.clicked.connect(self.export_json)
        root.addWidget(self.json_btn)
        self.export_btn.setEnabled(False)
        root.addWidget(self.export_btn)
        root.addStretch()

    def reset(self) -> None:
        """Clear progress and disable export until the new hash batch is complete."""
        self.progress.setValue(0)
        self.progress.setFormat("0%")
        self.export_btn.setEnabled(False)
        self.progress_changed.emit(0, "New analysis loaded. Report data reset.")

    def set_ready(self, ready: bool = True) -> None:
        self.export_btn.setEnabled(bool(ready))

    def _set_progress(self, value: int, message: str = "") -> None:
        value = max(0, min(100, int(value)))
        self.progress.setValue(value)
        self.progress.setFormat(f"{value}%")
        self.progress_changed.emit(value, message or f"Progress {value}%")

    def _default_pdf_name(self, current_file: str | None, hash_results: list) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        useful_results = [r for r in (hash_results or []) if getattr(r, "file_name", "") not in {".DS_Store", "Thumbs.db", "desktop.ini"}]
        if useful_results:
            if len(useful_results) == 1:
                base = Path(getattr(useful_results[0], "file_name", "PAC_HashTools_Report")).stem
                return f"{self._safe_filename(base)}_hash_report_{stamp}.pdf"
            first = Path(getattr(useful_results[0], "file_name", "Batch")).stem
            return f"{self._safe_filename(first)}_{len(useful_results)}files_hash_report_{stamp}.pdf"
        if current_file:
            base = Path(current_file).stem
            return f"{self._safe_filename(base)}_hash_report_{stamp}.pdf"
        return f"PAC_HashTools_Report_{stamp}.pdf"

    @staticmethod
    def _safe_filename(value: str) -> str:
        value = re.sub(r"[\\/:*?\"<>|]+", "_", str(value)).strip().strip(".")
        value = re.sub(r"\s+", "_", value)
        return value[:90] or "PAC_HashTools_Report"

    def export(self):
        current_file, metadata_rows, hash_results = self.get_state()
        if not hash_results:
            QMessageBox.information(
                self,
                "PDF Export",
                "No completed hash results are available for the current analysis. Please load files and wait for hashing to finish.",
            )
            return
        default_name = self._default_pdf_name(current_file, hash_results)
        default_path = str(Path.home() / default_name)
        out, _ = QFileDialog.getSaveFileName(self, "Save PDF Report", default_path, "PDF Files (*.pdf)")
        if not out:
            return
        if not out.lower().endswith(".pdf"):
            out += ".pdf"
        try:
            self._set_progress(0, "Starting PDF export...")
            PDFReportService().generate(out, current_file, metadata_rows, hash_results, progress_callback=self._set_progress, algorithms=self.get_algorithms())
            QMessageBox.information(self, "PDF Export", f"Saved PDF report:\n{out}")
        except Exception as exc:
            self._set_progress(0, "PDF export failed.")
            QMessageBox.critical(self, "PDF Export Error", f"Could not export PDF report:\n{exc}")

    def _export_thread_running(self) -> bool:
        """A Python wrapper may survive after Qt deletes its QThread."""
        thread = self._thread
        if thread is None:
            return False
        try:
            if sip.isdeleted(thread):
                self._thread = None
                self._worker = None
                return False
            return bool(thread.isRunning())
        except RuntimeError:
            self._thread = None
            self._worker = None
            return False

    def _export_thread_finished(self) -> None:
        """Release stale wrappers after a completed PDF export."""
        self._thread = None
        self._worker = None
        self.all_btn.setEnabled(not self._json_exporting)
        self.json_btn.setEnabled(not self._json_exporting)

    def export_all(self):
        db_path = self.get_hash_database()
        if not db_path or not os.path.isfile(db_path):
            QMessageBox.warning(self, "Export All", "No stored hash database is available. Complete hashing first.")
            return
        out, _ = QFileDialog.getSaveFileName(self, "Export all hash results", str(Path.home() / "HashTools_All_Hashes.pdf"), "PDF Files (*.pdf)")
        if not out:
            return
        if not out.lower().endswith('.pdf'):
            out += '.pdf'
        if self._export_thread_running():
            QMessageBox.information(self, "Export busy", "Wait for the current export to finish.")
            return
        self.all_btn.setEnabled(False)
        self.json_btn.setEnabled(False)
        self._thread = QThread(self)
        self._worker = FullPDFWorker(db_path, out, self.get_algorithms())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._set_progress)
        self._worker.completed.connect(lambda path, count: QMessageBox.information(self, "Export Complete", f"Exported {count:,} stored hashes to:\n{path}"))
        self._worker.failed.connect(lambda err: QMessageBox.critical(self, "PDF Export Error", err))
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._export_thread_finished)
        self._thread.start()

    def export_json(self):
        """Export from the stored database without involving a QThread/QObject worker.

        The export is incremental and pumps UI events periodically; this avoids
        the native macOS Qt crash caused by thread/worker teardown races.
        """
        from PyQt6.QtWidgets import QApplication

        db_path = self.get_hash_database()
        if not db_path or not os.path.isfile(db_path):
            QMessageBox.warning(self, "JSON Export", "Complete hashing before exporting.")
            return
        if self._json_exporting or (self._thread is not None and self._thread.isRunning()):
            QMessageBox.information(self, "Export busy", "Wait for the current export to finish.")
            return
        algorithms = tuple(self.get_algorithms())
        if not algorithms:
            QMessageBox.warning(self, "JSON Export", "Select at least one hash algorithm.")
            return
        out, _ = QFileDialog.getSaveFileName(
            self, "Export selected hashes as JSON",
            str(Path.home() / "HashTools_Hashes.json"), "JSON Files (*.json)"
        )
        if not out:
            return
        if not out.lower().endswith(".json"):
            out += ".json"
        self._json_exporting = True
        self.json_btn.setEnabled(False)
        self.all_btn.setEnabled(False)
        self._set_progress(0, "Starting JSON export...")
        try:
            class UIProgress:
                def emit(inner_self, value, message):
                    self._set_progress(value, message)
                    QApplication.processEvents()

            count = export_json_hashes(db_path, out, algorithms, UIProgress())
        except Exception as exc:
            self._set_progress(0, "JSON export failed")
            QMessageBox.critical(self, "JSON Export Error", str(exc))
        else:
            QMessageBox.information(self, "JSON Export", f"Exported {count:,} records to:\n{out}")
        finally:
            self._json_exporting = False
            self.all_btn.setEnabled(True)
            self.json_btn.setEnabled(True)


def export_json_hashes(db_path, output, algorithms, progress_signal):
    """Stream a valid JSON document without loading the database into memory."""
    algorithms = tuple(algorithms)
    if not algorithms or any(a not in ("md5", "sha1", "sha256", "sha512") for a in algorithms):
        raise ValueError("Invalid hash algorithm selection")
    target = Path(output)
    fd, temp = tempfile.mkstemp(prefix=".hash_export_", suffix=".json", dir=target.parent)
    try:
        with sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True) as db, os.fdopen(fd, "w", encoding="utf-8") as f:
            total = db.execute("SELECT COUNT(*) FROM results").fetchone()[0]
            f.write('{"application":"HashTools","generated_at":')
            json.dump(datetime.now().astimezone().isoformat(), f)
            f.write(',"algorithms":')
            json.dump(list(algorithms), f)
            f.write(',"total_files":' + str(total) + ',"results":[')
            columns = ("path", "size", "success", "error", "warning") + algorithms
            cursor = db.execute("SELECT " + ",".join(columns) + " FROM results ORDER BY id")
            for index, row in enumerate(cursor):
                item = dict(zip(columns, row))
                item["file_name"] = Path(item["path"] or "").name
                item["success"] = bool(item["success"])
                item["hashes"] = {a.upper(): item.pop(a) for a in algorithms}
                if index:
                    f.write(",")
                json.dump(item, f, ensure_ascii=False)
                if index % 250 == 0:
                    progress_signal.emit(int((index + 1) * 100 / max(total, 1)), f"JSON export: {index + 1:,}/{total:,}")
            f.write("]}")
        os.replace(temp, target)
        progress_signal.emit(100, "JSON export completed")
        return total
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
