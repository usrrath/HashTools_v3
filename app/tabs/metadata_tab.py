from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QFileDialog,
    QComboBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
)

from services.metadata_service import MetadataService
from utils.file_utils import collect_files
from utils.ui_utils import make_button


class MetadataTab(QWidget):
    file_selected = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.rows: list[tuple[str, str]] = []
        self.files: list[str] = []
        self.current_file: str | None = None
        self.service = MetadataService()
        self._updating_selector = False
        self._build()

    def _build(self):
        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.file_combo = QComboBox()
        self.file_combo.setMinimumWidth(320)
        self.file_combo.setToolTip("Select any file from the current uploaded batch")
        self.file_combo.currentIndexChanged.connect(self._on_file_changed)

        self.path = QLabel("No file selected")
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        btn = make_button("📂 Select File")
        btn.clicked.connect(self.choose)

        bar.addWidget(QLabel("File:"))
        bar.addWidget(self.file_combo, 1)
        bar.addWidget(btn)
        root.addLayout(bar)
        root.addWidget(self.path)

        self.gps_label = QLabel("GPS: Select an image file to check latitude/longitude.")
        self.gps_label.setOpenExternalLinks(True)
        self.gps_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        root.addWidget(self.gps_label)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Property", "Value"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(self._open_link_from_cell)
        root.addWidget(self.table)

    def choose(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select file", str(Path.home()), "All Files (*.*)")
        if path:
            self.file_selected.emit(path)

    def clear(self) -> None:
        self.files = []
        self.current_file = None
        self.rows = []
        self._updating_selector = True
        self.file_combo.clear()
        self._updating_selector = False
        self.path.setText("No file selected")
        self.gps_label.setText("GPS: Select an image file to check latitude/longitude.")
        self.table.setRowCount(0)

    def set_files(self, paths: list[str], selected_path: str | None = None, scanned: bool = False) -> None:
        files = list(paths) if scanned else collect_files(paths)
        self.clear()
        if not files:
            return

        self.files = files
        self._updating_selector = True
        for file_path in files:
            self.file_combo.addItem(Path(file_path).name, file_path)
        self._updating_selector = False

        target = selected_path if selected_path in files else files[0]
        index = self.file_combo.findData(target)
        self.file_combo.setCurrentIndex(max(0, index))
        self.load_file(target, update_selector=False)

    def load_file(self, path: str, update_selector: bool = True):
        if not path or not Path(path).is_file():
            return

        self.current_file = str(Path(path).resolve())
        self.path.setText(self.current_file)

        if update_selector:
            if self.current_file not in self.files:
                self.files.append(self.current_file)
                self._updating_selector = True
                self.file_combo.addItem(Path(self.current_file).name, self.current_file)
                self._updating_selector = False
            index = self.file_combo.findData(self.current_file)
            if index >= 0 and index != self.file_combo.currentIndex():
                self._updating_selector = True
                self.file_combo.setCurrentIndex(index)
                self._updating_selector = False

        try:
            self.rows = self.service.extract(self.current_file)
        except Exception as exc:
            self.rows = [("[Error] Metadata", str(exc))]

        self.table.setRowCount(len(self.rows))
        gps_url = ""
        gps_lat = ""
        gps_lon = ""

        for r, (key, value) in enumerate(self.rows):
            key_item = QTableWidgetItem(str(key))
            value_item = QTableWidgetItem(str(value))

            if str(key) == "[GPS] Google Maps" and str(value).startswith("https://"):
                gps_url = str(value)
                value_item.setText(f"Open Google Maps: {value}")
                value_item.setToolTip("Double-click to open in Google Maps")
            elif str(key) == "[GPS] Latitude":
                gps_lat = str(value)
            elif str(key) == "[GPS] Longitude":
                gps_lon = str(value)

            self.table.setItem(r, 0, key_item)
            self.table.setItem(r, 1, value_item)

        if gps_url and gps_lat and gps_lon:
            self.gps_label.setText(
                f"GPS: Latitude {gps_lat}, Longitude {gps_lon} — "
                f"<a href='{gps_url}'>Open in Google Maps</a>"
            )
        else:
            self.gps_label.setText("GPS: No image GPS latitude/longitude found.")

    def _on_file_changed(self, index: int) -> None:
        if self._updating_selector or index < 0:
            return
        path = self.file_combo.itemData(index)
        if path:
            self.load_file(str(path), update_selector=False)

    def _open_link_from_cell(self, row: int, column: int):
        if column != 1:
            return
        key_item = self.table.item(row, 0)
        value_item = self.table.item(row, 1)
        if not key_item or not value_item:
            return
        if key_item.text() == "[GPS] Google Maps":
            text = value_item.text().replace("Open Google Maps: ", "").strip()
            if text.startswith("https://"):
                QDesktopServices.openUrl(QUrl(text))
