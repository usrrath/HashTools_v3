"""Read-only, bounded universal preview for the Hex Inspector.

Office conversion and evidence copying run off the GUI thread. All artifacts live
in a private temporary directory and are removed when the widget closes.
"""
from __future__ import annotations

import mimetypes
import math
import shutil
import tempfile
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QPlainTextEdit, QLabel, QPushButton, QScrollArea)

try:
    import pymupdf as fitz
except ImportError:
    fitz = None
try:
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PyQt6.QtMultimediaWidgets import QVideoWidget
except ImportError:
    QMediaPlayer = QAudioOutput = QVideoWidget = None

OFFICE = {'.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odp', '.odt', '.ods', '.pps', '.ppsx'}
IMAGES = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tif', '.tiff', '.webp'}
MEDIA = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.mp3', '.wav', '.m4a', '.flac', '.ogg'}
TEXT = {'.txt', '.log', '.conf', '.ini', '.json', '.xml', '.csv', '.py', '.md', '.yaml', '.yml', '.html', '.css', '.js', '.sh', '.bat', '.ps1'}
MAX_COPY = 300 * 1024 * 1024


class PreviewWorker(QThread):
    ready = pyqtSignal(int, str, str, str)  # token, kind, temp path, error

    def __init__(self, token: int, source: str, folder: str):
        super().__init__()
        self.token, self.source, self.folder = token, source, folder

    def run(self):
        try:
            src = Path(self.source)
            if not src.is_file():
                raise FileNotFoundError(self.source)
            if src.stat().st_size > MAX_COPY:
                raise ValueError('Preview copy exceeds 300 MiB safety limit; hex inspection remains available')
            workspace = Path(self.folder)
            workspace.mkdir(parents=True, exist_ok=True)
            copied = workspace / ('evidence' + src.suffix.lower())
            with src.open('rb') as read, copied.open('wb') as write:
                shutil.copyfileobj(read, write, 1024 * 1024)
            kind = 'office' if src.suffix.lower() in OFFICE else 'file'
            if kind == 'office':
                from services.office_conversion_service import convert_office
                copied = convert_office(copied, workspace / 'conversion', 'pdf', '.pdf')
            self.ready.emit(self.token, kind, str(copied), '')
        except Exception as exc:
            self.ready.emit(self.token, '', '', str(exc))


class UniversalPreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._token = 0
        self._workers = []
        self._workspaces = []
        self._pdf = None
        self._page = 0
        self._zoom = 1.0
        self._image_path = None
        self._media_path = None
        self._current_source = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("universalPreviewTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(True)
        layout.addWidget(self.tabs, 1)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlaceholderText('Select a text file')
        self.tabs.addTab(self.text, 'Text')
        self.office = QLabel('Select an Office document')
        self.office.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.office.setWordWrap(True)
        self.office_page = self._page_view(self.office)
        self.tabs.addTab(self.office_page, 'Office Viewer')
        self.image = QLabel('Select an image')
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_page = self._page_view(self.image)
        self.tabs.addTab(self.image_page, 'Image')
        self.hex = QPlainTextEdit()
        self.hex.setReadOnly(True)
        self.tabs.addTab(self.hex, 'Hex')
        self.pdf_label = QLabel('Select a PDF')
        self.pdf_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pdf_page = self._page_view(self.pdf_label)
        self.tabs.addTab(self.pdf_page, 'PDF')
        media_page = QWidget()
        media_layout = QVBoxLayout(media_page)
        self.media_status = QLabel('Select a video or audio file')
        self.media_status.setWordWrap(True)
        media_layout.addWidget(self.media_status)
        self.player = None
        if QMediaPlayer and QVideoWidget:
            self.player = QMediaPlayer(self)
            self.audio = QAudioOutput(self)
            self.player.setAudioOutput(self.audio)
            self.video = QVideoWidget()
            self.player.setVideoOutput(self.video)
            media_layout.addWidget(self.video, 1)
            buttons = QHBoxLayout()
            play = QPushButton('Play')
            pause = QPushButton('Pause')
            stop = QPushButton('Stop')
            play.clicked.connect(self.player.play)
            pause.clicked.connect(self.player.pause)
            stop.clicked.connect(self.player.stop)
            for button in (play, pause, stop):
                buttons.addWidget(button)
            media_layout.addLayout(buttons)
        else:
            media_layout.addWidget(QLabel('Qt Multimedia is unavailable'))
        self.tabs.addTab(media_page, 'Video/Audio')
        self.tabs.setStyleSheet("QTabBar::tab { padding: 8px 11px; min-height: 24px; } QTabBar::tab:selected { color: #0866dc; border-top: 3px solid #0866dc; font-weight: bold; }")
        self.details = QLabel("Select a file to inspect")
        self.details.setWordWrap(True)
        self.details.setStyleSheet("padding:8px;border:1px solid #d5dee8;border-radius:6px;")
        layout.addWidget(self.details, 0)
        controls = QHBoxLayout()
        self.previous = QPushButton('◀ Previous')
        self.next = QPushButton('Next ▶')
        self.counter = QLabel('Page 0 / 0')
        self.fit = QPushButton('Fit page')
        self.zoom_out = QPushButton('−')
        self.zoom_in = QPushButton('+')
        for widget in (self.previous, self.counter, self.next, self.zoom_out, self.zoom_in, self.fit):
            controls.addWidget(widget)
        layout.addLayout(controls)
        self.previous.clicked.connect(lambda: self._move(-1))
        self.next.clicked.connect(lambda: self._move(1))
        self.fit.clicked.connect(self._fit)
        self.zoom_out.clicked.connect(lambda: self._scale(0.8))
        self.zoom_in.clicked.connect(lambda: self._scale(1.25))
        self.tabs.currentChanged.connect(lambda _: self._render_pdf())

    @staticmethod
    def _page_view(label):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(label)
        scroll.setMinimumHeight(220)
        return scroll


    def reset(self):
        """Clear the shared viewer without deleting a running worker's workspace."""
        self._token += 1
        self._current_source = None
        if self.player:
            self.player.stop()
            self.player.setSource(QUrl())
        if self._pdf is not None:
            self._pdf.close()
            self._pdf = None
        self.text.clear()
        self.hex.clear()
        for label in (self.office, self.image, self.pdf_label):
            label.clear()
            label.setText('Select a file to preview')
        self.details.setText('Select a file to inspect')
        self.counter.setText('Page 0 / 0')
        self.tabs.setCurrentIndex(0)

    def load_file(self, path: str):
        self._token += 1
        token = self._token
        self._current_source = path
        source = Path(path)
        try:
            stat = source.stat()
            self.details.setText(f"Name: {source.name}\nSource path: {source}\nSize: {stat.st_size:,} bytes\nExtension: {source.suffix or '(none)'}\nPreview source: isolated temporary copy")
        except OSError as exc:
            self.details.setText(f"Source: {path}\nError: {exc}")
        if self._pdf is not None:
            self._pdf.close()
        self._pdf = None
        self._page = 0
        self._zoom = 1.0
        self._image_path = None
        if self.player:
            self.player.stop()
            self.player.setSource(QUrl())
        self.office.setText('Loading Office preview…')
        self.pdf_label.setText('Loading PDF preview…')
        self.image.setText('Loading image…')
        self.media_status.setText('Loading media…')
        self.text.clear()
        self.hex.clear()
        ext = Path(path).suffix.lower()
        target = 1 if ext in OFFICE else 2 if ext in IMAGES else 4 if ext == '.pdf' else 5 if ext in MEDIA else 3 if self._is_binary(path) else 0
        self.tabs.setCurrentIndex(target)
        workspace = tempfile.TemporaryDirectory(prefix='hashtools_preview_')
        self._workspaces.append(workspace)
        worker = PreviewWorker(token, path, workspace.name)
        self._workers.append(worker)
        worker.ready.connect(self._ready)
        worker.finished.connect(lambda w=worker: self._release_worker(w))
        worker.start()

    @staticmethod
    def _is_binary(path: str) -> bool:
        try:
            with open(path, 'rb') as stream:
                data = stream.read(8192)
            if not data:
                return False
            if b'\x00' in data:
                return True
            try:
                data.decode('utf-8-sig')
            except UnicodeDecodeError:
                return True
            controls = sum(b < 32 and b not in (9, 10, 12, 13) for b in data)
            return controls / len(data) > 0.02
        except OSError:
            return True

    def _release_worker(self, worker):
        if worker in self._workers:
            self._workers.remove(worker)
        # Keep temporary data for the current preview; release obsolete copies.
        if not self._workers and len(self._workspaces) > 1:
            old = self._workspaces[:-1]
            self._workspaces = self._workspaces[-1:]
            for workspace in old:
                workspace.cleanup()

    def _ready(self, token, kind, path, error):
        if token != self._token:
            return
        if error:
            for label in (self.office, self.pdf_label, self.image, self.media_status):
                label.setText('Preview unavailable: ' + error)
            self.text.setPlainText('Preview unavailable: ' + error)
            self.hex.setPlainText('Preview unavailable: ' + error)
            return
        source = Path(self._current_source)
        suffix = source.suffix.lower()
        try:
            with open(path if kind == 'file' else self._current_source, 'rb') as fh:
                head = fh.read(4096)
            self.hex.setPlainText('\n'.join(f'{i:08X}  '+ ' '.join(f'{b:02X}' for b in head[i:i+16]) for i in range(0, len(head), 16)))
            if not self._is_binary(path) and suffix not in OFFICE | IMAGES | MEDIA | {'.pdf'}:
                with open(path, 'rb') as fh:
                    raw = fh.read(1024 * 1024)
                self.text.setPlainText(raw.decode('utf-8-sig', errors='replace'))
            else:
                self.text.setPlainText('Binary or non-text data. Use the Hex preview to inspect bytes safely.')
            if suffix in OFFICE or suffix == '.pdf':
                if fitz is None:
                    raise RuntimeError('PyMuPDF is not installed')
                self._pdf = fitz.open(path)
                self._page = 0
                self._render_pdf()
            elif suffix in IMAGES:
                self._image_path = path
                self._render_image()
            elif suffix in MEDIA:
                self._media_path = path
                if self.player:
                    self.player.setSource(QUrl.fromLocalFile(path))
                    self.media_status.setText(source.name)
                else:
                    self.media_status.setText('Qt Multimedia not installed')
        except Exception as exc:
            self.text.setPlainText(str(exc))
            self.office.setText(str(exc))
            self.pdf_label.setText(str(exc))

    def _render_image(self):
        if not self._image_path:
            return
        pix = QPixmap(self._image_path)
        if pix.isNull():
            self.image.setText('Unsupported image')
            return
        area = self.image_page.viewport().size()
        self.image.setPixmap(pix.scaled(area, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def _render_pdf(self):
        if self._pdf is None or not self._pdf.page_count:
            return
        target = self.office if Path(self._current_source).suffix.lower() in OFFICE else self.pdf_label
        try:
            page = self._pdf.load_page(self._page)
            area = (self.office_page if target is self.office else self.pdf_page).viewport().size()
            factor = min(max(0.2, (area.width()-16) / page.rect.width), max(0.2, (area.height()-16) / page.rect.height)) * self._zoom
            factor = min(3.0, max(0.15, factor))
            pix = page.get_pixmap(matrix=fitz.Matrix(factor, factor), alpha=False)
            from PyQt6.QtGui import QImage
            image = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
            target.setPixmap(QPixmap.fromImage(image))
            self.counter.setText(f'Page {self._page + 1} / {self._pdf.page_count}')
        except Exception as exc:
            target.setText('PDF rendering error: ' + str(exc))

    def _move(self, delta):
        if self._pdf:
            self._page = max(0, min(self._pdf.page_count - 1, self._page + delta))
            self._render_pdf()

    def _scale(self, factor):
        self._zoom = max(0.25, min(4.0, self._zoom * factor))
        self._render_pdf()

    def _fit(self):
        self._zoom = 1.0
        self._render_pdf()
        self._render_image()

    def closeEvent(self, event):
        if self.player:
            self.player.stop()
        for worker in self._workers:
            worker.wait(150000)
        if self._pdf is not None:
            self._pdf.close()
        self._pdf = None
        for workspace in self._workspaces:
            workspace.cleanup()
        super().closeEvent(event)
