"""Volatility analysis pane embedded only inside the Memory Capture tab."""
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,QPushButton,
    QFileDialog,QComboBox,QPlainTextEdit,QMessageBox)
from services.memory.volatility_adapter import PLUGINS, availability, run_plugin, plugin_dependencies

class VolatilityWorker(QThread):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)
    def __init__(self, image, plugin):
        super().__init__()
        self.image, self.plugin = image, plugin
        self.stop_event = threading.Event()
    def run(self):
        try:
            raw = run_plugin(self.image, self.plugin, cancelled=self.stop_event.is_set)
            try: result = json.loads(raw)
            except json.JSONDecodeError: raise RuntimeError("Volatility returned non-JSON output: " + raw[-2000:])
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")

class VolatilityCapturePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self.last_result = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Volatility 3 — Offline memory image analysis (not RAM acquisition)"))
        row = QHBoxLayout()
        self.image = QLineEdit()
        self.image.setPlaceholderText("Choose acquired .raw / .mem / .dmp / .vmem / .lime / .aff4 image")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        row.addWidget(self.image, 1)
        row.addWidget(browse)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.plugin = QComboBox()
        self.plugin.addItems(PLUGINS)
        self.run_button = QPushButton("Analyze")
        self.run_button.clicked.connect(self.start)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        self.export_button = QPushButton("Export JSON…")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export)
        row.addWidget(self.plugin, 1)
        row.addWidget(self.run_button)
        row.addWidget(self.cancel_button)
        row.addWidget(self.export_button)
        layout.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output, 1)
        self.refresh()
    def refresh(self):
        ok, reason = availability()
        missing = [name for name, installed in plugin_dependencies().items() if not installed]
        self.status.setText(reason + (" | Optional modules missing: " + ", ".join(missing) if missing else ""))
        self.run_button.setEnabled(ok and not (self.worker and self.worker.isRunning()))
    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select memory image", "", "Memory images (*.raw *.mem *.ram *.dmp *.vmem *.lime *.aff4);;All files (*)")
        if path: self.image.setText(path)
    def start(self):
        if self.worker and self.worker.isRunning(): return
        path = Path(self.image.text()).expanduser()
        if not path.is_file():
            QMessageBox.warning(self, "Missing image", "Select an existing memory image.")
            return
        self.last_result = None
        self.export_button.setEnabled(False)
        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status.setText("Analyzing image (read-only)…")
        self.output.clear()
        self.worker = VolatilityWorker(str(path), self.plugin.currentText())
        self.worker.completed.connect(self.done)
        self.worker.failed.connect(self.failed)
        self.worker.finished.connect(self.finished)
        self.worker.start()
    def cancel(self):
        if self.worker: self.worker.stop_event.set()
        self.status.setText("Cancellation requested…")
    def done(self, result):
        self.last_result = result
        rendered = json.dumps(result, indent=2, default=str)
        self.output.setPlainText(rendered[:1500000] + ("\n[Preview truncated; export contains full result]" if len(rendered)>1500000 else ""))
        self.export_button.setEnabled(True)
        self.status.setText("Analysis completed. Results are not independently verified.")
    def failed(self, message):
        self.status.setText("Analysis failed — inspect details")
        self.output.setPlainText(message)
    def finished(self):
        self.cancel_button.setEnabled(False)
        self.refresh_buttons()
    def refresh_buttons(self):
        self.run_button.setEnabled(availability()[0])
    def export(self):
        if self.last_result is None: return
        path, _ = QFileDialog.getSaveFileName(self, "Export analysis", "volatility_analysis.json", "JSON (*.json)")
        if not path: return
        source = Path(self.image.text()).resolve()
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(4*1024*1024), b""): digest.update(chunk)
        record = {"tool":"Volatility 3", "plugin":self.plugin.currentText(),
                  "image":str(source), "image_sha256":digest.hexdigest(),
                  "exported_utc":datetime.now(timezone.utc).isoformat(), "results":self.last_result}
        Path(path).write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        self.status.setText("Export saved: " + path)
