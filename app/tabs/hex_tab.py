from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPixmap
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QGridLayout, QLabel, QLineEdit,
    QComboBox, QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog,
    QFrame, QSplitter, QMessageBox, QSizePolicy
)

from config.settings import HEX_BYTES_PER_ROW, HEX_PAGE_SIZE, SUPPORTED_IMAGES
from utils.file_utils import collect_files, human_size, guess_file_type
from utils.hex_utils import read_page, ascii_char, query_to_bytes, find_bytes, file_signature_from_path
from utils.ui_utils import make_button
from app.tabs.universal_preview import UniversalPreview


class HexViewTab(QWidget):
    file_loaded = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.files: list[str] = []
        self.current_file: str | None = None
        self.offset = 0
        self.file_size = 0
        self.page_data = b""
        self._updating_selector = False
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        t = QGridLayout(toolbar)
        t.setContentsMargins(10, 10, 10, 10)

        self.file_combo = QComboBox()
        self.file_combo.setMinimumWidth(280)
        self.file_combo.setToolTip("Select any file from the current uploaded batch")
        self.file_combo.currentIndexChanged.connect(self._on_file_changed)

        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("Open a file or select one from the uploaded batch...")
        self.open_btn = make_button("📂 Open Binary")
        self.open_btn.clicked.connect(self.choose_file)
        self.export_btn = make_button("💾 Export Hex TXT")
        self.export_btn.clicked.connect(self.export_hex_txt)

        self.search_mode = QComboBox()
        self.search_mode.addItems(["Text (UTF-8)", "Hex Bytes"])
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Find text or hex, e.g. PNG or 89 50 4E 47")
        self.find_btn = make_button("🔍 Find Next")
        self.find_btn.clicked.connect(self.find_next)
        self.jump_edit = QLineEdit()
        self.jump_edit.setPlaceholderText("Offset hex, e.g. 1A0")
        self.jump_btn = make_button("Go")
        self.jump_btn.clicked.connect(self.jump_to_offset)
        self.prev_btn = make_button("◀ Prev Page")
        self.prev_btn.clicked.connect(self.prev_page)
        self.next_btn = make_button("Next Page ▶")
        self.next_btn.clicked.connect(self.next_page)
        self.show_ascii = QCheckBox("Show ASCII")
        self.show_ascii.setChecked(True)
        self.show_ascii.stateChanged.connect(self.render)
        self.highlight_sig = QCheckBox("Highlight File Signature")
        self.highlight_sig.setChecked(True)
        self.highlight_sig.stateChanged.connect(self.render)
        self.highlight_null = QCheckBox("Highlight 00 Bytes")
        self.highlight_null.setChecked(True)
        self.highlight_null.stateChanged.connect(self.render)

        t.addWidget(QLabel("File:"), 0, 0)
        t.addWidget(self.file_combo, 0, 1, 1, 2)
        t.addWidget(self.path_edit, 0, 3, 1, 3)
        t.addWidget(self.open_btn, 0, 6)
        t.addWidget(self.export_btn, 0, 7)
        t.addWidget(QLabel("Find:"), 1, 0)
        t.addWidget(self.search_mode, 1, 1)
        t.addWidget(self.search_edit, 1, 2, 1, 3)
        t.addWidget(self.find_btn, 1, 5)
        t.addWidget(QLabel("Jump:"), 1, 6)
        t.addWidget(self.jump_edit, 1, 7)
        t.addWidget(self.jump_btn, 1, 8)
        t.addWidget(self.prev_btn, 2, 0, 1, 2)
        t.addWidget(self.next_btn, 2, 2, 1, 2)
        t.addWidget(self.show_ascii, 2, 4)
        t.addWidget(self.highlight_sig, 2, 5, 1, 2)
        t.addWidget(self.highlight_null, 2, 7, 1, 2)
        root.addWidget(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([
            "Offset (hex)",
            "Hex Bytes: 00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F",
            "ASCII",
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setFont(QFont("Menlo", 12))
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.update_selected_status)
        splitter.addWidget(self.table)

        right = QFrame()
        right.setObjectName("sidePanel")
        rv = QVBoxLayout(right)
        preview_heading = QLabel("Temporary copy preview")
        preview_heading.setObjectName("hexPreviewHeading")
        preview_heading.setStyleSheet("font-weight:700;font-size:15px;padding:3px;")
        rv.addWidget(preview_heading)
        self.universal_preview = UniversalPreview(right)
        self.universal_preview.setObjectName("hexUniversalSixTabPreview")
        self.universal_preview.setMinimumHeight(300)
        # The six-tab preview must be the actual right-hand panel, not an image-only placeholder.
        assert self.universal_preview.tabs.count() == 6, "Universal preview did not initialize all six tabs"
        assert [self.universal_preview.tabs.tabText(i) for i in range(6)] == [
            "Text", "Office Viewer", "Image", "Hex", "PDF", "Video/Audio"
        ]
        rv.addWidget(self.universal_preview, 8)
        # Compact, responsive file-details card. Avoid fixed-height multiline labels:
        # Qt/macOS font metrics can otherwise clip the last rows.
        self.details_card = QFrame(right)
        self.details_card.setObjectName("hexFileDetailsCard")
        self.details_card.setStyleSheet("""
            QFrame#hexFileDetailsCard {
                background: #ffffff;
                border: 1px solid #dbe3ed;
                border-radius: 10px;
            }
            QFrame#hexFileDetailsCard QLabel {
                border: none;
                background: transparent;
                padding: 0;
                color: #172033;
                font-size: 12px;
            }
        """)
        details_layout = QVBoxLayout(self.details_card)
        details_layout.setContentsMargins(12, 10, 12, 10)
        details_layout.setSpacing(7)

        details_title = QLabel("File details", self.details_card)
        details_title.setStyleSheet("font-weight: 700; font-size: 13px;")
        details_layout.addWidget(details_title)

        self.detail_name = QLabel("No file selected", self.details_card)
        self.detail_name.setWordWrap(True)
        self.detail_name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail_name.setStyleSheet("font-weight: 600; font-size: 12px;")
        details_layout.addWidget(self.detail_name)

        details_grid = QGridLayout()
        details_grid.setHorizontalSpacing(16)
        details_grid.setVerticalSpacing(4)
        self.detail_values = {}
        for i, (key, caption) in enumerate((
            ("size", "Size"), ("bytes", "Total bytes"),
            ("offset", "Current offset"), ("type", "File type"),
            ("signature", "Signature"),
        )):
            r, c = divmod(i, 2)
            heading = QLabel(caption, self.details_card)
            heading.setStyleSheet("color: #64748b; font-size: 11px;")
            value = QLabel("—", self.details_card)
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            details_grid.addWidget(heading, r * 2, c)
            details_grid.addWidget(value, r * 2 + 1, c)
            self.detail_values[key] = value
        details_grid.setColumnStretch(0, 1)
        details_grid.setColumnStretch(1, 1)
        details_layout.addLayout(details_grid)
        rv.addWidget(self.details_card, 0)

        self.byte_info = QLabel("Selected row\nHex: —  |  Dec: —  |  ASCII: —  |  Binary: —")
        self.byte_info.setWordWrap(True)
        self.byte_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.byte_info.setStyleSheet("""
            background: #111827; color: #e5e7eb; border-radius: 10px;
            padding: 10px; font-family: Menlo, monospace; font-size: 11px;
        """)
        self.byte_info.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        rv.addWidget(self.byte_info, 0)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 5)
        right.setMinimumWidth(450)
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([800, 800])
        self._hex_splitter = splitter
        root.addWidget(splitter, 1)

        self.status = QLabel("Open a file to view its bytes.")
        self.status.setStyleSheet("padding:6px;color:#334155;")
        root.addWidget(self.status)
        self._set_navigation_enabled(False)

    def choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open image or binary file", str(Path.home()), "All Files (*.*)")
        if path:
            self.file_loaded.emit(path)

    def clear(self) -> None:
        self.files = []
        self.current_file = None
        self.offset = 0
        self.file_size = 0
        self.page_data = b""
        self._updating_selector = True
        self.file_combo.clear()
        self._updating_selector = False
        self.path_edit.clear()
        self.table.setRowCount(0)
        self.universal_preview.reset()
        self._update_file_details()
        self.byte_info.setText("Selected Byte\nHex: -\nDec: -\nASCII: -\nBinary: -")
        self.status.setText("Open a file to view its bytes.")
        self._set_navigation_enabled(False)

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
        self.load_file(target, emit_signal=False, update_selector=False)

    def load_file(self, path: str, emit_signal: bool = False, update_selector: bool = True) -> None:
        if not path or not Path(path).is_file():
            return

        resolved = str(Path(path).resolve())
        self.current_file = resolved
        self.offset = 0
        self.file_size = 0
        self.page_data = b""
        self.path_edit.setText(resolved)

        if update_selector:
            if resolved not in self.files:
                self.files.append(resolved)
                self._updating_selector = True
                self.file_combo.addItem(Path(resolved).name, resolved)
                self._updating_selector = False
            index = self.file_combo.findData(resolved)
            if index >= 0 and index != self.file_combo.currentIndex():
                self._updating_selector = True
                self.file_combo.setCurrentIndex(index)
                self._updating_selector = False

        if emit_signal:
            self.file_loaded.emit(resolved)
        self.load_preview()
        self.render()

    def _on_file_changed(self, index: int) -> None:
        if self._updating_selector or index < 0:
            return
        path = self.file_combo.itemData(index)
        if path:
            self.load_file(str(path), emit_signal=False, update_selector=False)

    def load_preview(self) -> None:
        if self.current_file:
            self.universal_preview.load_file(self.current_file)
        else:
            self.universal_preview.reset()

    def _update_file_details(self, signature: str | None = None) -> None:
        """Refresh compact file metadata without forcing a fixed widget height."""
        if not self.current_file:
            self.detail_name.setText("No file selected")
            self.detail_name.setToolTip("")
            for label in self.detail_values.values():
                label.setText("—")
            return

        path = Path(self.current_file)
        self.detail_name.setText(path.name)
        self.detail_name.setToolTip(str(path))
        values = {
            "size": human_size(self.file_size),
            "bytes": f"{self.file_size:,}",
            "offset": f"0x{self.offset:08X}",
            "type": str(guess_file_type(self.current_file)),
            "signature": str(signature if signature is not None else "—"),
        }
        for key, value in values.items():
            self.detail_values[key].setText(value)
            self.detail_values[key].setToolTip(value)

    def render(self) -> None:
        if not self.current_file:
            return
        try:
            self.page_data, self.offset, self.file_size = read_page(self.current_file, self.offset, HEX_PAGE_SIZE)
            rows = (len(self.page_data) + HEX_BYTES_PER_ROW - 1) // HEX_BYTES_PER_ROW
            self.table.setRowCount(rows)
            for row in range(rows):
                absolute = self.offset + row * HEX_BYTES_PER_ROW
                chunk = self.page_data[row * HEX_BYTES_PER_ROW:(row + 1) * HEX_BYTES_PER_ROW]
                offset_item = QTableWidgetItem(f"{absolute:08X}")
                hex_item = QTableWidgetItem("  ".join(f"{b:02X}" for b in chunk))
                ascii_item = QTableWidgetItem("".join(ascii_char(b) for b in chunk) if self.show_ascii.isChecked() else "")
                for item in (offset_item, hex_item, ascii_item):
                    item.setFont(QFont("Menlo", 12))
                    item.setForeground(QColor("#e5e7eb"))
                    item.setBackground(QColor("#1f2937" if row % 2 == 0 else "#111827"))
                if self.highlight_sig.isChecked() and absolute < 16:
                    for item in (offset_item, hex_item, ascii_item):
                        item.setBackground(QColor("#1e3a8a"))
                if self.highlight_null.isChecked() and chunk.count(0) >= 6:
                    hex_item.setForeground(QColor("#94a3b8"))
                self.table.setItem(row, 0, offset_item)
                self.table.setItem(row, 1, hex_item)
                self.table.setItem(row, 2, ascii_item)

            sig = file_signature_from_path(self.current_file)
            self._update_file_details(signature=sig)
            self.status.setText(
                f"{Path(self.current_file).name} — showing 0x{self.offset:08X} - "
                f"0x{min(self.offset + len(self.page_data), self.file_size):08X} of {self.file_size:,} bytes"
            )
            self.prev_btn.setEnabled(self.offset > 0)
            self.next_btn.setEnabled(self.offset + HEX_PAGE_SIZE < self.file_size)
            self.find_btn.setEnabled(True)
            self.jump_btn.setEnabled(True)
            self.export_btn.setEnabled(True)
        except Exception as exc:
            self.table.setRowCount(0)
            self.status.setText(f"Could not read {Path(self.current_file).name}")
            QMessageBox.critical(self, "Hex View Error", str(exc))

    def _set_navigation_enabled(self, enabled: bool) -> None:
        self.prev_btn.setEnabled(enabled)
        self.next_btn.setEnabled(enabled)
        self.find_btn.setEnabled(enabled)
        self.jump_btn.setEnabled(enabled)
        self.export_btn.setEnabled(enabled)

    def next_page(self) -> None:
        if self.current_file:
            self.offset = min(self.offset + HEX_PAGE_SIZE, max(0, self.file_size - 1))
            self.render()

    def prev_page(self) -> None:
        if self.current_file:
            self.offset = max(0, self.offset - HEX_PAGE_SIZE)
            self.render()

    def jump_to_offset(self) -> None:
        if not self.current_file:
            return
        text = self.jump_edit.text().strip().lower().replace("0x", "")
        try:
            self.offset = int(text, 16)
            self.render()
        except ValueError:
            QMessageBox.warning(self, "Invalid Offset", "Enter a hexadecimal offset, for example: 1A0")

    def find_next(self) -> None:
        if not self.current_file:
            return
        try:
            needle = query_to_bytes(self.search_edit.text(), self.search_mode.currentText())
            if not needle:
                QMessageBox.information(self, "Find", "Enter text or hex bytes to search for.")
                return
            pos = find_bytes(self.current_file, needle, self.offset + 1)
            if pos == -1:
                QMessageBox.information(self, "Find", "Pattern not found.")
                return
            self.offset = pos - (pos % HEX_BYTES_PER_ROW)
            self.render()
            row = (pos - self.offset) // HEX_BYTES_PER_ROW
            if 0 <= row < self.table.rowCount():
                self.table.selectRow(row)
            self.status.setText(f"Found in {Path(self.current_file).name} at offset 0x{pos:08X}")
        except Exception as exc:
            QMessageBox.warning(self, "Find Error", str(exc))

    def update_selected_status(self) -> None:
        if not self.table.selectedItems() or not self.page_data:
            return
        row = self.table.currentRow()
        idx = row * HEX_BYTES_PER_ROW
        if 0 <= idx < len(self.page_data):
            b = self.page_data[idx]
            self.byte_info.setText(
                f"Selected Row\nOffset: 0x{self.offset + idx:08X}\n"
                f"First Byte Hex: {b:02X}\nDec: {b}\nASCII: {ascii_char(b)}\nBinary: {b:08b}"
            )

    def export_hex_txt(self) -> None:
        if not self.current_file:
            return
        default_name = f"{Path(self.current_file).stem}_hex_0x{self.offset:08X}.txt"
        out, _ = QFileDialog.getSaveFileName(
            self,
            "Export current hex page",
            str(Path.home() / default_name),
            "Text Files (*.txt)",
        )
        if not out:
            return
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"File: {self.current_file}\n")
            f.write(f"Offset: 0x{self.offset:08X}\n\n")
            for row in range(self.table.rowCount()):
                f.write(
                    f"{self.table.item(row, 0).text()}  {self.table.item(row, 1).text()}  "
                    f"|{self.table.item(row, 2).text()}|\n"
                )
        QMessageBox.information(self, "Export", f"Saved: {out}")
