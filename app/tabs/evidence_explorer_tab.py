"""Read-only forensic explorer. All preview decoders consume a temporary copy."""
from __future__ import annotations

import datetime
import html
import hashlib
import mimetypes
import os
import sys
from pathlib import Path
import shutil
import stat
import tempfile
import time
import threading
import zipfile
import xml.etree.ElementTree as ET

from config.settings import SEARCH_PAGE_SIZE
from services.screen_recording import ScreenRecordingWorker
from services.search_index import FileIndex, QueryError, compile_search, parse_query  # noqa: F401 (compile_search re-exported)
from PyQt6.QtCore import Qt, QDir, QUrl, QThread, pyqtSignal, QTimer, QRectF, QEvent
from PyQt6.QtGui import QPixmap, QImage, QPainter
from app.models.folder_tree_model import EvidenceFolderModel
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QCheckBox, QComboBox, QFileDialog, QSplitter, QTreeView,
    QTableWidget, QTableWidgetItem, QHeaderView, QTabWidget, QPlainTextEdit,
    QScrollArea, QMessageBox, QProgressBar, QTextBrowser, QFrame, QSizePolicy, QGraphicsScene, QGraphicsView)

try:
    from PyQt6.QtPdf import QPdfDocument
    from PyQt6.QtPdfWidgets import QPdfView
except ImportError:
    QPdfDocument = QPdfView = None
try:
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PyQt6.QtMultimediaWidgets import QVideoWidget, QGraphicsVideoItem
except ImportError:
    QMediaPlayer = QAudioOutput = QVideoWidget = QGraphicsVideoItem = None

PREVIEW_LIMIT = 250 * 1024 * 1024
TEXT_LIMIT = 2 * 1024 * 1024
HEX_LIMIT = 65536
OFFICE_TEXT_LIMIT = 200_000


def office_preview(path: Path) -> str:
    """Dependency-free, bounded OOXML text preview from a preservation copy.

    Does not execute macros, formulas, external links or embedded content.
    """
    suffix = path.suffix.lower()
    lines = [f'{suffix.upper()[1:]} — Extracted Text Preview', 'Text content only; original page layout is not reproduced.', '']
    chars = len(lines[0])
    max_chars = OFFICE_TEXT_LIMIT
    namespaces = {
        'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
        'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
        'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
    }

    def append(line):
        nonlocal chars
        if chars >= max_chars:
            return False
        line = str(line)
        lines.append(line[:max_chars - chars])
        chars += min(len(line), max_chars - chars) + 1
        return chars < max_chars

    with zipfile.ZipFile(path) as z:
        entries = z.infolist()
        if len(entries) > 20000 or sum(i.file_size for i in entries) > 512 * 1024 * 1024:
            raise ValueError('Office archive exceeds preview safety limits')
        names = set(z.namelist())

        def xml(name):
            if name not in names:
                return None
            info = z.getinfo(name)
            if info.file_size > 32 * 1024 * 1024:
                raise ValueError('Office XML part too large for preview')
            return ET.fromstring(z.read(name))

        if suffix == '.docx':
            root = xml('word/document.xml')
            if root is None:
                raise ValueError('Word document content not found')
            for paragraph in root.findall('.//w:p', namespaces):
                value = ''.join(t.text or '' for t in paragraph.findall('.//w:t', namespaces))
                if value.strip() and not append(value):
                    break
        elif suffix == '.pptx':
            import re
            slide_names = sorted((n for n in names if re.fullmatch(r'ppt/slides/slide\d+\.xml', n)),
                                 key=lambda n: int(re.search(r'slide(\d+)', n).group(1)))
            for number, name in enumerate(slide_names, 1):
                if not append(f'\n[SLIDE {number}]'):
                    break
                root = xml(name)
                for paragraph in root.findall('.//a:p', namespaces):
                    value = ''.join(t.text or '' for t in paragraph.findall('.//a:t', namespaces))
                    if value.strip() and not append(value):
                        break
                if chars >= max_chars:
                    break
        elif suffix == '.xlsx':
            import re
            shared = []
            shared_root = xml('xl/sharedStrings.xml')
            if shared_root is not None:
                for si in shared_root.findall('x:si', namespaces):
                    shared.append(''.join(t.text or '' for t in si.findall('.//x:t', namespaces)))
            sheet_names = sorted((n for n in names if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n)),
                                 key=lambda n: int(re.search(r'sheet(\d+)', n).group(1)))
            for number, name in enumerate(sheet_names, 1):
                if not append(f'\n[SHEET {number}]'):
                    break
                root = xml(name)
                for row in root.findall('.//x:sheetData/x:row', namespaces):
                    cells = []
                    for cell in row.findall('x:c', namespaces):
                        address = cell.get('r', '?')
                        kind = cell.get('t', '')
                        value_node = cell.find('x:v', namespaces)
                        inline_node = cell.find('x:is', namespaces)
                        formula = cell.find('x:f', namespaces)
                        value = value_node.text if value_node is not None else None
                        if kind == 's' and value is not None:
                            try:
                                value = shared[int(value)]
                            except (ValueError, IndexError):
                                pass
                        elif kind == 'inlineStr' and inline_node is not None:
                            value = ''.join(t.text or '' for t in inline_node.findall('.//x:t', namespaces))
                        if formula is not None:
                            value = f'FORMULA: {formula.text or ""} (cached: {value or ""})'
                        if value is not None:
                            cells.append(f'{address}: {value}')
                    if cells and not append(' | '.join(cells)):
                        break
                if chars >= max_chars:
                    break
        else:
            raise ValueError('Unsupported Office extension')
    if chars >= max_chars:
        lines.append('[Preview truncated at 200,000 characters]')
    return '\n'.join(lines)



def office_preview_html(extracted: str) -> str:
    """Readable, non-executable HTML presentation of already extracted Office text."""
    lines = extracted.splitlines()
    blocks = []
    cells = []
    def flush_cells():
        if cells:
            blocks.append('<table>' + ''.join('<tr><th>' + html.escape(k) + '</th><td>' + html.escape(v) + '</td></tr>' for k,v in cells) + '</table>')
            cells.clear()
    for index, line in enumerate(lines):
        line = line.strip()
        if not line:
            flush_cells()
            continue
        if line.startswith('[SLIDE ') or line.startswith('[SHEET '):
            flush_cells()
            blocks.append('<h2>' + html.escape(line.strip('[]')) + '</h2>')
        elif line.startswith(('DOCX —', 'PPTX —', 'XLSX —', 'LEGACY OFFICE —')):
            flush_cells()
            blocks.append('<h1>' + html.escape(line) + '</h1>')
        elif ' | ' in line and ':' in line and len(line) < 5000:
            flush_cells()
            blocks.append('<p class="cells">' + html.escape(line) + '</p>')
        elif ':' in line and line.split(':',1)[0].strip().replace('$','').isalnum() and len(line) < 2000:
            k,v=line.split(':',1)
            cells.append((k.strip(),v.strip()))
            if len(cells)>=80: flush_cells()
        else:
            flush_cells()
            blocks.append('<p>' + html.escape(line) + '</p>')
    flush_cells()
    css = """<style>
      body { font-family: "Helvetica Neue", "Helvetica", "Khmer Sangam MN"; font-size: 12pt; color:#202936; margin:18px; }
      h1 { font-size:17pt; color:#184b82; border-bottom:2px solid #c9d8e9; padding-bottom:9px; }
      h2 { font-size:14pt; color:#155e85; background:#eaf3fb; padding:10px; margin-top:20px; }
      p { line-height:1.5; margin:7px 0; white-space:pre-wrap; }
      table { border-collapse:collapse; width:100%; margin:10px 0 18px; }
      th { background:#edf3f9; text-align:left; width:25%; }
      th,td { border:1px solid #ccd5df; padding:6px; vertical-align:top; }
      .cells { background:#f4f7fa; padding:8px; }
      </style>"""
    return '<html><head>' + css + '</head><body>' + ''.join(blocks) + '</body></html>'


def legacy_office_preview(path: Path) -> str:
    """Extract legacy Office text only from an isolated temporary copy."""
    from services.office_conversion_service import convert_office
    formats = {'.doc': ('docx', '.docx'), '.ppt': ('pptx', '.pptx'),
               '.xls': ('xlsx', '.xlsx')}
    fmt, extension = formats[path.suffix.lower()]
    with tempfile.TemporaryDirectory(prefix='hashtool_office_') as scratch:
        output = convert_office(path, Path(scratch), fmt, extension, timeout=45)
        if output.stat().st_size > PREVIEW_LIMIT:
            raise ValueError('Converted Office document exceeds preview limit')
        extracted = office_preview(output)
        return ('LEGACY OFFICE — EXTRACTED TEXT ONLY (NOT ORIGINAL LAYOUT)\n'
                'Converted from a temporary copy using LibreOffice.\n\n' + extracted)


def readable_size(size):
    value = float(size)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.2f} {unit}'
        value /= 1024


def hidden(path: Path):
    if path.name.startswith('.'):
        return True
    try:
        attrs = path.stat().st_file_attributes
        return bool(attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))
    except (AttributeError, OSError):
        return False


class SearchPageWorker(QThread):
    """Answer queries from the in-memory index off the GUI thread; the newest request wins."""
    page_ready = pyqtSignal(int, int, int, object, str, float)  # token, page, total, rows, error, seconds

    def __init__(self, parent=None):
        super().__init__(parent)
        self._condition = threading.Condition()
        self._request = None
        self._closing = False
        self._last = None  # (index, total, query, filter, hidden, result): paging reuses the result

    def request(self, token, index, query, name_filter, include_hidden, page, page_size):
        with self._condition:
            self._request = (token, index, query, name_filter, include_hidden, page, page_size)
            self._condition.notify()

    def shutdown(self):
        with self._condition:
            self._closing = True
            self._condition.notify()

    def _superseded(self):
        return self._request is not None or self._closing

    def run(self):
        while True:
            with self._condition:
                while self._request is None and not self._closing:
                    self._condition.wait()
                if self._closing:
                    return
                token, index, query, name_filter, include_hidden, page, page_size = self._request
                self._request = None
            rows, count, error, seconds = [], 0, '', 0.0
            try:
                last = self._last
                if last and last[0] is index and last[1:5] == (index.total, query, name_filter, include_hidden):
                    result = last[5]
                else:
                    result = index.search(query, include_hidden, name_filter, cancel=self._superseded)
                    self._last = (index, index.total, query, name_filter, include_hidden, result)
                count, seconds = result.total, result.elapsed
                page = min(page, max(0, (count - 1) // page_size))
                rows = result.rows(page * page_size, page_size)
            except InterruptedError:
                continue  # a newer request is already waiting
            except (QueryError, OSError, ValueError) as exc:
                error = str(exc)
            self.page_ready.emit(token, page, count, rows, error, seconds)


class EvidenceSearchWorker(QThread):
    """Scan the tree ONCE into a FileIndex; every later query is answered from memory."""
    progress = pyqtSignal(int, int)
    results_available = pyqtSignal(int, int)  # indexed items, indexed items (new data is queryable)
    completed = pyqtSignal(bool, int, int, str)

    def __init__(self, index, parent=None):
        super().__init__(parent)
        self.index = index
        self.cancelled = False

    def stop(self):
        self.cancelled = True

    def run(self):
        error = ''

        def report(total, done):
            if not done:
                self.progress.emit(total, 0)
                self.results_available.emit(total, total)
        try:
            self.index.build(cancel=lambda: self.cancelled, progress=report)
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
        self.completed.emit(self.cancelled, self.index.total, self.index.total, error or self.index.error)


class EvidenceExplorerTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.root = Path.home()
        self.current = self.root
        self.selected = None
        self.temp_dir = tempfile.TemporaryDirectory(prefix='hashtools_evidence_preview_')
        self.preview_path = None
        self.pdf_doc = None
        self.player = None
        self.audio = None
        self.history = []
        self.search_worker = None
        self.recording_worker = None
        self.search_mode = False
        self.file_index = None
        self.search_query = ''
        self._last_search_seconds = 0.0
        self.search_page = 0
        self.search_total = 0
        self.search_page_size = SEARCH_PAGE_SIZE
        self._page_token = 0
        self._page_worker = SearchPageWorker(self)
        self._page_worker.page_ready.connect(self._search_page_ready)
        self._page_worker.start()
        self._search_filter_timer = QTimer(self)
        self._search_filter_timer.setSingleShot(True)
        self._search_filter_timer.setInterval(450)
        self._search_filter_timer.timeout.connect(self._apply_search_filter)
        self._search_type_timer = QTimer(self)  # search-as-you-type once an index exists
        self._search_type_timer.setSingleShot(True)
        self._search_type_timer.setInterval(150)
        self._search_type_timer.timeout.connect(lambda: self.search_everything(typed=True))
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(300)
        self._preview_timer.timeout.connect(self._load_selected_preview)
        self._selection_path = None
        self._visible_search_total = 0
        self._last_live_refresh = 0.0
        self._live_refresh_timer = QTimer(self)
        self._live_refresh_timer.setSingleShot(True)
        self._live_refresh_timer.timeout.connect(self._refresh_live_search)
        self.search_scanned = 0
        self._build_ui()
        self.navigate(self.root)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        warning = QLabel('READ-ONLY EVIDENCE EXPLORER — Originals are never modified. Preview uses a temporary preservation copy.')
        warning.setWordWrap(True)
        layout.addWidget(warning)
        recording_row = QHBoxLayout()
        recording_row.addWidget(QLabel('Screen Evidence (primary display; no audio)'))
        self.record_start = QPushButton('● Start Recording')
        self.record_pause = QPushButton('Pause')
        self.record_stop = QPushButton('■ Stop & Save')
        self.record_pause.setEnabled(False)
        self.record_stop.setEnabled(False)
        self.record_start.clicked.connect(self.start_screen_recording)
        self.record_pause.clicked.connect(self.toggle_screen_pause)
        self.record_stop.clicked.connect(self.stop_screen_recording)
        for button in (self.record_start, self.record_pause, self.record_stop):
            recording_row.addWidget(button)
        layout.addLayout(recording_row)
        self.record_status = QLabel('Idle — recording starts only when you click Start.')
        self.record_status.setWordWrap(True)
        layout.addWidget(self.record_status)
        root_row = QHBoxLayout()
        root_row.addWidget(QLabel('Browse Root'))
        self.root_input = QLineEdit(str(self.root))
        root_row.addWidget(self.root_input, 1)
        browse = QPushButton('Browse…')
        browse.clicked.connect(self.choose_root)
        root_row.addWidget(browse)
        apply_btn = QPushButton('Open Root')
        apply_btn.clicked.connect(self.open_root)
        root_row.addWidget(apply_btn)
        layout.addLayout(root_row)
        nav = QHBoxLayout()
        for label, fn in [('Home', self.go_home), ('Back', self.go_back), ('Parent Folder', self.go_parent)]:
            btn = QPushButton(label)
            btn.clicked.connect(fn)
            nav.addWidget(btn)
        self.location = QLineEdit()
        self.location.setReadOnly(True)
        nav.addWidget(self.location, 1)
        layout.addLayout(nav)
        options = QHBoxLayout()
        self.show_hidden = QCheckBox('Show hidden/system items')
        self.show_hidden.toggled.connect(self._hidden_toggled)
        options.addWidget(self.show_hidden)
        options.addWidget(QLabel('Filename filter'))
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText('Filter current folder by filename…')
        self.filter_input.textChanged.connect(self._filter_changed)
        options.addWidget(self.filter_input, 1)
        refresh = QPushButton('Refresh')
        refresh.clicked.connect(self.refresh)
        options.addWidget(refresh)
        layout.addLayout(options)
        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText('Everything search: report  *.exe  ext:pdf;docx  parent:cases  size:>10mb  dm:>2024-01-01  !draft  a | b  regex:"^IMG_\\d+"')
        self.search_input.setToolTip(
            'space = AND, | = OR, ! = NOT, "quoted phrase"\n'
            'name contains: report      wildcards: *.pdf  rep?rt*      a term with / matches the full path\n'
            'ext:pdf;docx   parent:cases/2024   path:cases   file:   folder:\n'
            'size:>10mb  size:1kb..5mb   dm:2024-05-03  dm:>2024-01-01   regex:"^IMG_\\d+"')
        self.search_input.returnPressed.connect(self.search_everything)
        self.search_input.textChanged.connect(self._search_typed)
        search_row.addWidget(self.search_input, 1)
        self.search_button = QPushButton('🔎 Search Everything')
        self.search_button.clicked.connect(self.search_everything)
        search_row.addWidget(self.search_button)
        self.stop_search_button = QPushButton('Stop Search')
        self.stop_search_button.setEnabled(False)
        self.stop_search_button.clicked.connect(self.stop_search)
        search_row.addWidget(self.stop_search_button)
        self.reindex_button = QPushButton('Re-index')
        self.reindex_button.setToolTip('Scan the folder again (needed only if files changed since the last index)')
        self.reindex_button.clicked.connect(lambda: self.search_everything(reindex=True))
        search_row.addWidget(self.reindex_button)
        self.clear_search_button = QPushButton('Clear Results')
        self.clear_search_button.clicked.connect(self.clear_search)
        search_row.addWidget(self.clear_search_button)
        layout.addLayout(search_row)
        self.search_status = QLabel('Search all subfolders by file or folder name. The first search indexes the folder; after that results are instant. Evidence stays read-only.')
        layout.addWidget(self.search_status)
        self.search_progress_bar = QProgressBar(self)
        self.search_progress_bar.setRange(0, 0)  # Unknown total: indeterminate, never fake percent
        self.search_progress_bar.setFormat('Indexing filesystem…')
        self.search_progress_bar.hide()
        layout.addWidget(self.search_progress_bar)
        page_row = QHBoxLayout()
        self.prev_page_button = QPushButton(f'◀ Previous {SEARCH_PAGE_SIZE}')
        self.prev_page_button.clicked.connect(lambda: self.show_search_page(self.search_page - 1))
        self.next_page_button = QPushButton(f'Next {SEARCH_PAGE_SIZE} ▶')
        self.next_page_button.clicked.connect(lambda: self.show_search_page(self.search_page + 1))
        self.page_status = QLabel('')
        page_row.addWidget(self.prev_page_button)
        page_row.addWidget(self.page_status, 1)
        page_row.addWidget(self.next_page_button)
        layout.addLayout(page_row)
        self.prev_page_button.hide()
        self.next_page_button.hide()
        main_split = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(main_split, 1)
        left = QWidget()
        left_l = QVBoxLayout(left)
        left_l.setContentsMargins(0, 0, 0, 0)
        left_l.addWidget(QLabel('Drives / Volumes'))
        drive_row = QHBoxLayout()
        self.drive_selector = QComboBox()
        self.drive_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.drive_selector.setMinimumContentsLength(18)
        drive_row.addWidget(self.drive_selector, 1)
        refresh_drives = QPushButton('Refresh')
        refresh_drives.clicked.connect(self.refresh_drives)
        drive_row.addWidget(refresh_drives)
        left_l.addLayout(drive_row)
        left_l.addWidget(QLabel('Folder tree'))
        self.model = EvidenceFolderModel(self)
        self.model.setFilter(QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot | QDir.Filter.Drives)
        self.tree = QTreeView()
        self.tree.setUniformRowHeights(True)
        self.tree.setAnimated(False)
        self.tree.setSortingEnabled(False)
        self.tree.setModel(self.model)
        # Use Qt native checkbox painting/editing; custom delegate caused blank squares.
        self.tree.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self.tree.setMinimumWidth(300)
        self.tree.setIndentation(18)
        self.tree.setHeaderHidden(True)
        self.tree.setExpandsOnDoubleClick(True)
        self.model.setRootPath(str(self.root))
        self.drive_selector.currentIndexChanged.connect(self.drive_changed)
        self.refresh_drives()
        for column in (1, 2, 3):
            self.tree.hideColumn(column)
        # Single-click on a checkbox only changes its check state.
        # Folder navigation uses double-click to avoid expensive refreshes per toggle.
        self.tree.doubleClicked.connect(self.tree_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        left_l.addWidget(self.tree)
        selection_row = QHBoxLayout()
        self.folder_selection_status = QLabel('0 folders selected')
        selection_row.addWidget(self.folder_selection_status, 1)
        clear_folders = QPushButton('Clear checks')
        clear_folders.clicked.connect(self.clear_folder_checks)
        selection_row.addWidget(clear_folders)
        left_l.addLayout(selection_row)
        self.model.dataChanged.connect(self.update_folder_selection_status)
        main_split.addWidget(left)
        middle = QWidget()
        mid_l = QVBoxLayout(middle)
        mid_l.setContentsMargins(0, 0, 0, 0)
        mid_l.addWidget(QLabel('File list (double-click a folder to open)'))
        self.files = QTableWidget(0, 4)
        self.files.setColumnCount(4)
        self.files.setHorizontalHeaderLabels(['Name', 'Size', 'Type', 'Location'])
        # Never elide long or Khmer Unicode filenames into '...'.
        # Columns remain user-resizable; horizontal scrolling exposes full text.
        self.files.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.files.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        header = self.files.horizontalHeader()
        for column in range(4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        self.files.setColumnWidth(0, 440)
        self.files.setColumnWidth(1, 115)
        self.files.setColumnWidth(2, 95)
        self.files.setColumnWidth(3, 420)
        self.files.setWordWrap(False)
        self.files.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.files.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.files.itemSelectionChanged.connect(self.select_file)
        self.files.itemDoubleClicked.connect(self.double_click)
        mid_l.addWidget(self.files)
        main_split.addWidget(middle)
        right = QWidget()
        right_l = QVBoxLayout(right)
        right_l.setContentsMargins(0, 0, 0, 0)
        right_l.addWidget(QLabel('Temporary copy preview'))
        self.preview_tabs = QTabWidget()
        self.text_preview = QPlainTextEdit()
        self.text_preview.setReadOnly(True)
        self.hex_preview = QPlainTextEdit()
        self.hex_preview.setReadOnly(True)
        self.image_label = QLabel('Select an image')
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image_scroll = QScrollArea()
        image_scroll.setWidgetResizable(True)
        image_scroll.setWidget(self.image_label)
        self.preview_tabs.addTab(self.text_preview, 'Text')
        self.office_preview_widget = QTextBrowser()
        self.office_preview_widget.setReadOnly(True)
        # Real page/slide/sheet layout preview via LibreOffice PDF conversion.
        # The QTextBrowser remains a fallback when LibreOffice is unavailable.
        self.office_layout_container = QWidget()
        office_layout = QVBoxLayout(self.office_layout_container)
        office_buttons = QHBoxLayout()
        self.office_mode = QPushButton('Rendered pages')
        self.office_text_mode = QPushButton('Extracted text')
        self.office_page_info = QLabel('Page 0 / 0')
        self.office_previous = QPushButton('◀')
        self.office_next = QPushButton('▶')
        self.office_fit = QPushButton('Fit width')
        self.office_zoom_out = QPushButton('−')
        self.office_zoom_in = QPushButton('+')
        for b in (self.office_mode, self.office_text_mode, self.office_previous,
                  self.office_page_info, self.office_next, self.office_zoom_out,
                  self.office_zoom_in, self.office_fit):
            office_buttons.addWidget(b)
        office_layout.addLayout(office_buttons)
        # One rendered viewer: rasterized PDF pages include document images,
        # tables, charts and layout. No competing blank QtPdf tab.
        self.office_pages = QTabWidget()
        self.office_pdf_doc = None
        self.office_pdf_view = None
        self.office_raster_scroll = QScrollArea(self.office_pages)
        self.office_raster_scroll.setWidgetResizable(True)
        self.office_raster_scroll.setMinimumHeight(250)
        self.office_raster_scroll.viewport().installEventFilter(self)
        self.office_raster_host = QWidget()
        self.office_raster_layout = QVBoxLayout(self.office_raster_host)
        self.office_raster_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.office_raster_scroll.setWidget(self.office_raster_host)
        self.office_pages.addTab(self.office_raster_scroll, 'Rendered document + images')
        self.office_pages.addTab(self.office_preview_widget, 'Text fallback')
        self.office_render_pdf_path = None
        self.office_is_presentation = False
        self.office_render_page_start = 0
        self.office_render_page_count = 0
        self.office_render_zoom = 1.0
        self.office_fit_mode = 'page'
        self.office_render_diagnostic = ''
        office_layout.addWidget(self.office_pages)
        self.office_mode.clicked.connect(self.show_office_rendered)
        self.office_text_mode.clicked.connect(lambda: self.office_pages.setCurrentWidget(self.office_preview_widget))
        self.office_previous.clicked.connect(lambda: self.office_move(-1))
        self.office_next.clicked.connect(lambda: self.office_move(1))
        self.office_fit.clicked.connect(self.office_fit_pages)
        self.office_zoom_out.clicked.connect(lambda: self.office_zoom(1/1.25))
        self.office_zoom_in.clicked.connect(lambda: self.office_zoom(1.25))
        self.preview_tabs.addTab(self.office_layout_container, 'Office Viewer')
        self.preview_tabs.addTab(image_scroll, 'Image')
        self.preview_tabs.addTab(self.hex_preview, 'Hex')
        pdf_widget = QWidget()
        pdf_layout = QVBoxLayout(pdf_widget)
        if QPdfDocument and QPdfView:
            self.pdf_view = QPdfView(pdf_widget)
            self.pdf_doc = QPdfDocument(self)
            self.pdf_view.setDocument(self.pdf_doc)
            self.pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
            self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
            controls = QHBoxLayout()
            self.pdf_prev = QPushButton('◀ Previous')
            self.pdf_next = QPushButton('Next ▶')
            self.pdf_counter = QLabel('Page 0 / 0')
            self.pdf_zoom_out = QPushButton('−')
            self.pdf_zoom_in = QPushButton('+')
            self.pdf_fit = QPushButton('Fit width')
            for widget in (self.pdf_prev, self.pdf_counter, self.pdf_next,
                           self.pdf_zoom_out, self.pdf_zoom_in, self.pdf_fit):
                controls.addWidget(widget)
            pdf_layout.addLayout(controls)
            self.pdf_prev.clicked.connect(lambda: self.pdf_move(-1))
            self.pdf_next.clicked.connect(lambda: self.pdf_move(1))
            self.pdf_zoom_out.clicked.connect(lambda: self.pdf_zoom(1 / 1.25))
            self.pdf_zoom_in.clicked.connect(lambda: self.pdf_zoom(1.25))
            self.pdf_fit.clicked.connect(lambda: self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth))
            self.pdf_doc.pageCountChanged.connect(self.update_pdf_counter)
            self.pdf_view.pageNavigator().currentPageChanged.connect(self.update_pdf_counter)
            pdf_layout.addWidget(self.pdf_view)
        else:
            self.pdf_view = None
            self.pdf_doc = None
            self.pdf_fallback_path = None
            self.pdf_fallback_page = 0
            self.pdf_fallback_zoom = 1.0
            self.pdf_fallback_count = 0
            controls = QHBoxLayout()
            self.pdf_prev = QPushButton('◀ Previous')
            self.pdf_next = QPushButton('Next ▶')
            self.pdf_counter = QLabel('Page 0 / 0')
            self.pdf_zoom_out = QPushButton('−')
            self.pdf_zoom_in = QPushButton('+')
            self.pdf_fit = QPushButton('Fit width')
            for control in (self.pdf_prev, self.pdf_counter, self.pdf_next, self.pdf_zoom_out, self.pdf_zoom_in, self.pdf_fit):
                controls.addWidget(control)
            pdf_layout.addLayout(controls)
            self.pdf_fallback_scroll = QScrollArea(pdf_widget)
            self.pdf_fallback_scroll.setWidgetResizable(True)
            self.pdf_fallback_label = QLabel('Select a PDF to preview')
            self.pdf_fallback_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.pdf_fallback_scroll.setWidget(self.pdf_fallback_label)
            pdf_layout.addWidget(self.pdf_fallback_scroll)
            self.pdf_prev.clicked.connect(lambda: self.pdf_move(-1))
            self.pdf_next.clicked.connect(lambda: self.pdf_move(1))
            self.pdf_zoom_out.clicked.connect(lambda: self.pdf_zoom(1/1.25))
            self.pdf_zoom_in.clicked.connect(lambda: self.pdf_zoom(1.25))
            self.pdf_fit.clicked.connect(self.pdf_fit_fallback)
        self.preview_tabs.addTab(pdf_widget, 'PDF')
        media_widget = QWidget()
        media_layout = QVBoxLayout(media_widget)
        if QMediaPlayer and QGraphicsVideoItem and QAudioOutput:
            self.player = QMediaPlayer(self)
            self.audio = QAudioOutput(self)
            self.player.setAudioOutput(self.audio)
            # Graphics-based video avoids native QVideoWidget sizing and macOS
            # fullscreen window placement problems inside nested scroll areas.
            self.video_scene = QGraphicsScene(self)
            self.video_item = QGraphicsVideoItem()
            self.video_item.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
            self.video_scene.addItem(self.video_item)
            self.player.setVideoOutput(self.video_item)
            self.video_view = QGraphicsView(self.video_scene, media_widget)
            self.video_view.setBackgroundBrush(Qt.GlobalColor.black)
            self.video_view.setFrameShape(QFrame.Shape.NoFrame)
            self.video_view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            self.video_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.video_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.video_view.setMinimumHeight(160)
            self.video_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            media_layout.addWidget(self.video_view, 1)
            self.video_zoom_factor = 1.0
            self.video_fullscreen_view = None
            self.video_item.nativeSizeChanged.connect(self.update_video_size)
            self.video_view.viewport().installEventFilter(self)
            controls = QHBoxLayout()
            play = QPushButton('Play')
            play.clicked.connect(self.player.play)
            pause = QPushButton('Pause')
            pause.clicked.connect(self.player.pause)
            stop = QPushButton('Stop')
            stop.clicked.connect(self.player.stop)
            zoom_out = QPushButton('Zoom −')
            zoom_out.clicked.connect(lambda: self.set_video_zoom(self.video_zoom_factor / 1.25))
            zoom_in = QPushButton('Zoom +')
            zoom_in.clicked.connect(lambda: self.set_video_zoom(self.video_zoom_factor * 1.25))
            zoom_reset = QPushButton('Fit')
            zoom_reset.clicked.connect(lambda: self.set_video_zoom(1.0))
            fullscreen = QPushButton('Full Screen')
            fullscreen.clicked.connect(self.toggle_video_fullscreen)
            self.video_fullscreen_button = fullscreen
            for button in (play, pause, stop, zoom_out, zoom_in, zoom_reset, fullscreen):
                controls.addWidget(button)
            media_layout.addLayout(controls)
            QTimer.singleShot(0, self.update_video_size)
        else:
            media_layout.addWidget(QLabel('Media playback unavailable: install PyQt6 multimedia modules.'))
        self.preview_tabs.addTab(media_widget, 'Video/Audio')
        right_l.addWidget(self.preview_tabs, 5)
        right_l.addWidget(QLabel('Selected item metadata'))
        self.metadata = QPlainTextEdit()
        self.metadata.setReadOnly(True)
        right_l.addWidget(self.metadata, 1)
        self.metadata.setMaximumHeight(125)
        self.preview_status = QLabel('No file selected')
        self.preview_status.setWordWrap(True)
        right_l.addWidget(self.preview_status)
        main_split.addWidget(right)
        main_split.setSizes([340, 500, 550])

    def eventFilter(self, watched, event):
        if watched is getattr(getattr(self, 'office_raster_scroll', None), 'viewport', lambda: None)() and event.type() == QEvent.Type.Resize:
            if getattr(self, 'office_render_pdf_path', None) and getattr(self, 'office_fit_mode', '') in ('page', 'width'):
                QTimer.singleShot(80, self.office_show_raster_pages)
        view = getattr(self, 'video_view', None)
        if view is not None and watched is view.viewport() and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self.update_video_size)
        full = getattr(self, 'video_fullscreen_view', None)
        if full is not None and watched is full and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            full.close()
            return True
        if full is not None and watched is full and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self.update_video_size)
        if full is not None and watched is full and event.type() == QEvent.Type.Close:
            self.video_fullscreen_view = None
            self.video_fullscreen_button.setText('Full Screen')
            QTimer.singleShot(0, self.update_video_size)
        return super().eventFilter(watched, event)

    def update_video_size(self):
        view = getattr(self, 'video_view', None)
        item = getattr(self, 'video_item', None)
        if view is None or item is None:
            return
        size = item.nativeSize()
        if size.width() <= 0 or size.height() <= 0:
            # Until Qt reports video dimensions, use a neutral 16:9 placeholder.
            rect = QRectF(0, 0, 16, 9)
        else:
            rect = QRectF(0, 0, size.width(), size.height())
        item.setSize(rect.size())
        self.video_scene.setSceneRect(rect)
        view.resetTransform()
        view.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
        if self.video_zoom_factor != 1.0:
            view.scale(self.video_zoom_factor, self.video_zoom_factor)
        full = getattr(self, 'video_fullscreen_view', None)
        if full is not None:
            full.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)

    def set_video_zoom(self, factor):
        if getattr(self, 'video_view', None) is None:
            return
        self.video_zoom_factor = max(0.5, min(5.0, float(factor)))
        self.update_video_size()

    def toggle_video_fullscreen(self):
        if getattr(self, 'video_scene', None) is None:
            return
        full = self.video_fullscreen_view
        if full is not None:
            full.close()
            return
        full = QGraphicsView(self.video_scene)
        full.setWindowTitle('HashTool — Evidence Video Preview (Esc to exit)')
        full.setBackgroundBrush(Qt.GlobalColor.black)
        full.setFrameShape(QFrame.Shape.NoFrame)
        full.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        full.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        full.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        full.installEventFilter(self)
        full.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.video_fullscreen_view = full
        self.video_fullscreen_button.setText('Exit Full Screen')
        # A top-level graphics view stays on the current screen; unlike native
        # QVideoWidget fullscreen it does not detach a native video surface.
        full.showFullScreen()
        QTimer.singleShot(0, self.update_video_size)

    def refresh_drives(self):
        """Enumerate mount points only; never recursively scan a drive."""
        from PyQt6.QtCore import QStorageInfo
        current = str(self.root)
        candidates = {}
        if sys.platform == 'darwin':
            candidates['/'] = 'Macintosh / Startup Volume (/)'
            volumes = Path('/Volumes')
            if volumes.is_dir():
                try:
                    for item in volumes.iterdir():
                        if item.is_dir() and item.name != 'Macintosh HD':
                            candidates[str(item)] = f'{item.name} ({item})'
                except OSError:
                    pass
        elif os.name == 'nt':
            for storage in QStorageInfo.mountedVolumes():
                path = storage.rootPath()
                if path and storage.isValid() and storage.isReady():
                    name = storage.displayName() or path
                    candidates[path] = f'{name} ({path})'
        else:
            candidates['/'] = 'Filesystem (/)'
            for storage in QStorageInfo.mountedVolumes():
                path = storage.rootPath()
                if path and storage.isValid() and storage.isReady() and path.startswith(('/media/', '/mnt/', '/run/media/')):
                    candidates[path] = f'{storage.displayName() or path} ({path})'
        if current not in candidates:
            candidates[current] = f'Current evidence root ({current})'
        self.drive_selector.blockSignals(True)
        self.drive_selector.clear()
        for path, label in candidates.items():
            self.drive_selector.addItem(label, path)
        index = self.drive_selector.findData(current)
        self.drive_selector.setCurrentIndex(max(0, index))
        self.drive_selector.blockSignals(False)

    def drive_changed(self, index):
        if index < 0:
            return
        path = self.drive_selector.itemData(index)
        if not path or not Path(path).is_dir():
            QMessageBox.warning(self, 'Drive unavailable', 'This drive is no longer mounted or accessible.')
            return
        self.root_input.setText(path)
        self.open_root()

    def choose_root(self):
        directory = QFileDialog.getExistingDirectory(self, 'Select evidence browse root', str(self.root))
        if directory:
            self.root_input.setText(directory)
            self.open_root()

    def open_root(self):
        self._drop_index()
        self.search_mode = False
        path = Path(self.root_input.text()).expanduser()
        if not path.is_dir():
            QMessageBox.warning(self, 'Invalid folder', 'The selected root is not an accessible folder.')
            return
        self.root = path.resolve()
        self.history.clear()
        self.navigate(self.root, remember=False)

    def go_home(self):
        self.navigate(self.root)

    def go_back(self):
        if self.history:
            self.navigate(self.history.pop(), remember=False)

    def go_parent(self):
        if self.current != self.root and self.root in self.current.parents:
            self.navigate(self.current.parent)

    def navigate(self, path, remember=True):
        self.stop_search()
        self.search_mode = False
        try:
            path = Path(path).resolve()
            if not path.is_dir() or (path != self.root and self.root not in path.parents):
                return
            if remember and path != self.current:
                self.history.append(self.current)
            self.current = path
            self.location.setText(str(path))
            if self.model.rootPath() != str(self.root):
                self.model.setRootPath(str(self.root))
            self.tree.setRootIndex(self.model.index(str(self.root)))
            self.tree.setCurrentIndex(self.model.index(str(path)))
            self.refresh()
        except OSError as exc:
            self.preview_status.setText(f'Cannot open folder: {exc}')

    def tree_clicked(self, index):
        # Clicking a checkbox must not launch a directory listing or expensive scan.
        # Navigation remains available by selecting the folder label.
        if not index.isValid():
            return
        path = Path(self.model.filePath(index))
        if path.is_dir():
            self.navigate(path)

    def clear_folder_checks(self):
        self.model.clear_selected()
        self.update_folder_selection_status()

    def update_folder_selection_status(self, *_):
        self.folder_selection_status.setText(f'{len(self.model.selected_folders()):,} folders selected')

    def selected_evidence_folders(self):
        """Return checked folder paths; caller decides how to process them."""
        return self.model.selected_folders()

    def refresh(self, *_):
        filters = QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot | QDir.Filter.Drives
        if self.show_hidden.isChecked():
            filters |= QDir.Filter.Hidden | QDir.Filter.System
        self.model.setFilter(filters)
        self.refresh_files()

    def _filter_changed(self, *_):
        if self.search_mode:
            self._search_filter_timer.start()
        else:
            self.refresh_files()

    def _apply_search_filter(self):
        if self.search_mode:
            self.show_search_page(0)

    def _refresh_search_count(self):
        # Never execute a full-table SQLite COUNT on the GUI thread.
        if self.search_mode:
            self.show_search_page(self.search_page)

    def refresh_files(self, *_):
        if self.search_mode:
            return
        self.files.setRowCount(0)
        self.clear_preview()
        keyword = self.filter_input.text().casefold()
        try:
            entries = sorted(self.current.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        except OSError as exc:
            self.preview_status.setText(f'Cannot list folder: {exc}')
            return
        for path in entries:
            if not self.show_hidden.isChecked() and hidden(path):
                continue
            if keyword and keyword not in path.name.casefold():
                continue
            try:
                is_dir = path.is_dir()
                size = '' if is_dir else readable_size(path.stat().st_size)
            except OSError:
                is_dir, size = False, 'Unavailable'
            row = self.files.rowCount()
            self.files.insertRow(row)
            name = QTableWidgetItem(('📁 ' if is_dir else '📄 ') + path.name)
            name.setData(Qt.ItemDataRole.UserRole, str(path))
            name.setToolTip(path.name)
            self.files.setItem(row, 0, name)
            self.files.setItem(row, 1, QTableWidgetItem(size))
            self.files.setItem(row, 2, QTableWidgetItem('Folder' if is_dir else (path.suffix.lower() or 'File')))
            location_item = QTableWidgetItem(str(path.parent))
            location_item.setToolTip(str(path.parent))
            self.files.setItem(row, 3, location_item)

    def _hidden_toggled(self, *_):
        self.refresh()
        if self.search_mode:
            self.show_search_page(0)  # instant: hidden state is stored in the index

    def _search_typed(self, *_):
        if self.file_index is not None and self.search_input.text().strip():
            self._search_type_timer.start()

    def _drop_index(self):
        self.stop_search()
        self._search_type_timer.stop()
        self.file_index = None
        self._page_token += 1

    def _index_building(self):
        return self.search_worker is not None and self.search_worker.isRunning()

    def _update_search_status(self):
        index = self.file_index
        if index is None or not self.search_mode:
            return
        matches = f'{self._visible_search_total:,} matches'
        if self._index_building():
            self.search_status.setText(f'Indexing {self.root} … {self.search_scanned:,} items so far · {matches} (searchable now)')
        else:
            took = f' in {self._last_search_seconds * 1000:,.0f} ms' if self._last_search_seconds else ''
            state = 'Index' if index.complete else 'Partial index (stopped)'
            self.search_status.setText(f'{state}: {index.total:,} items scanned in {index.scan_seconds:.1f} s · {matches}{took}'
                                       + (f' · Error: {index.error}' if index.error else ''))

    def search_everything(self, *_, reindex=False, typed=False):
        keyword = self.search_input.text().strip()
        if not keyword:
            QMessageBox.information(self, 'Search Everything', 'Enter a search query, such as *.pdf or ext:docx.')
            return
        try:
            parse_query(keyword)
        except QueryError as exc:
            self.search_status.setText(f'Invalid search: {exc}')
            return
        if reindex:
            self._drop_index()
        elif self.file_index is not None and Path(self.file_index.root) != Path(self.root):
            self._drop_index()
        elif self.file_index is not None and not typed and not self.file_index.complete and not self._index_building():
            self._drop_index()  # an explicit search after Stop/an error re-scans instead of reusing a partial index
        self._search_type_timer.stop()
        self._preview_timer.stop()
        self._page_token += 1
        self.search_mode = True
        self.search_query = keyword
        self.clear_preview()
        self.search_page = 0
        if self.file_index is None:
            self._visible_search_total = 0
            self._last_live_refresh = 0.0
            self._live_refresh_timer.stop()
            self.search_scanned = 0
            self.files.setRowCount(0)
            self.prev_page_button.hide()
            self.next_page_button.hide()
            self.page_status.setText('Indexing…')
            self.search_progress_bar.show()
            self.stop_search_button.setEnabled(True)
            index = FileIndex(self.root)
            self.file_index = index
            worker = EvidenceSearchWorker(index, self)
            self.search_worker = worker
            worker.progress.connect(self.search_progress)
            worker.results_available.connect(self.search_results_available)
            worker.completed.connect(lambda c, n, m, e, idx=index: self.search_finished(c, n, m, e) if idx is self.file_index else None)
            worker.finished.connect(worker.deleteLater)
            self._update_search_status()
            worker.start()
        else:
            self.show_search_page(0)  # index already exists (or is still being built): instant

    def search_progress(self, scanned, found):
        if not self.search_mode:
            return
        self.search_scanned = scanned
        self._update_search_status()

    def search_results_available(self, scanned, committed):
        """New segments were published: they are queryable right away (live results while indexing)."""
        if not self.search_mode:
            return
        self.search_scanned = scanned
        self._update_search_status()
        if not self._live_refresh_timer.isActive():
            delay = max(0, int(350 - (time.monotonic() - self._last_live_refresh) * 1000))
            self._live_refresh_timer.start(delay)

    def _refresh_live_search(self):
        if not self.search_mode or self.file_index is None:
            return
        self._last_live_refresh = time.monotonic()
        self.show_search_page(self.search_page)

    def show_search_page(self, page):
        if not self.search_mode or self.file_index is None:
            return
        self._page_token += 1
        self._page_worker.request(self._page_token, self.file_index, self.search_query,
                                  self.filter_input.text().strip(), self.show_hidden.isChecked(),
                                  max(0, page), self.search_page_size)

    def _search_page_ready(self, token, page, count, rows, error, seconds):
        if token != self._page_token or not self.search_mode:
            return
        if error:
            self.search_status.setText('Cannot run search: ' + error)
            return
        self.search_page = page
        self._visible_search_total = count
        self._last_search_seconds = seconds
        pages = max(1, (count + self.search_page_size - 1) // self.search_page_size)
        self._preview_timer.stop()
        self._selection_path = None
        self.files.blockSignals(True)
        self.files.setUpdatesEnabled(False)
        try:
            self.files.setRowCount(len(rows))
            for row, (path, name, is_dir, size, modified) in enumerate(rows):
                item = QTableWidgetItem(('📁 ' if is_dir else '📄 ') + name)
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setToolTip(name)
                self.files.setItem(row, 0, item)
                self.files.setItem(row, 1, QTableWidgetItem('' if is_dir else readable_size(size)))
                self.files.setItem(row, 2, QTableWidgetItem('Folder' if is_dir else (Path(name).suffix.lower() or 'File')))
                location_item = QTableWidgetItem(str(Path(path).parent))
                location_item.setToolTip(str(Path(path).parent))
                self.files.setItem(row, 3, location_item)
        finally:
            self.files.setUpdatesEnabled(True)
            self.files.blockSignals(False)
        start = page * self.search_page_size + 1 if rows else 0
        self.page_status.setText(f'{start:,}–{start + len(rows) - 1 if rows else 0:,} of {count:,} matches · Page {page + 1:,}/{pages:,}')
        self.prev_page_button.setVisible(page > 0)
        self.next_page_button.setVisible(page + 1 < pages)
        self._update_search_status()

    def search_finished(self, cancelled, scanned, matches, error):
        self.stop_search_button.setEnabled(False)
        self.search_progress_bar.hide()
        self.search_worker = None
        self._live_refresh_timer.stop()
        if not self.search_mode:
            return
        self.show_search_page(self.search_page)  # final page + status, from the complete index

    def stop_search(self):
        if self.search_worker is not None and self.search_worker.isRunning():
            self.search_worker.stop()

    def clear_search(self):
        self.stop_search()
        self._page_token += 1
        self._preview_timer.stop()
        self._search_filter_timer.stop()
        self._search_type_timer.stop()
        self._live_refresh_timer.stop()
        self.search_progress_bar.hide()
        self.search_mode = False
        self.prev_page_button.hide()
        self.next_page_button.hide()
        self.page_status.setText('')
        self.search_status.setText('Search cleared. Showing current folder (the index is kept: the next search is instant).')
        self.refresh_files()

    def double_click(self, item):
        first = self.files.item(item.row(), 0)
        if first:
            path = Path(first.data(Qt.ItemDataRole.UserRole))
            if path.is_dir():
                self.navigate(path)

    def preview_view_loaded(self):
        if self.pdf_view:
            self.pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
            self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
            self.update_pdf_counter()

    def update_pdf_counter(self, *_args):
        if self.pdf_view is None or self.pdf_doc is None:
            return
        total = self.pdf_doc.pageCount()
        current = self.pdf_view.pageNavigator().currentPage() + 1 if total else 0
        self.pdf_counter.setText(f'Page {current} / {total}')
        self.pdf_prev.setEnabled(current > 1)
        self.pdf_next.setEnabled(current < total)

    def pdf_fit_fallback(self):
        if self.pdf_view:
            self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        else:
            self.pdf_fallback_zoom = 1.0
            self.pdf_render_fallback()

    def pdf_render_fallback(self):
        if not getattr(self, 'pdf_fallback_path', None):
            return
        try:
            try:
                import pymupdf as fitz
            except ImportError:
                import pymupdf as fitz
            with fitz.open(str(self.pdf_fallback_path)) as doc:
                count = len(doc)
                if not count:
                    raise ValueError('PDF contains no pages')
                self.pdf_fallback_count = count
                self.pdf_fallback_page = max(0, min(count-1, self.pdf_fallback_page))
                page = doc[self.pdf_fallback_page]
                width = max(280, self.pdf_fallback_scroll.viewport().width()-32)
                scale = min(3.0, max(0.2, width / max(1, page.rect.width) * self.pdf_fallback_zoom))
                scale = min(scale, 2400 / max(page.rect.width, page.rect.height))
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
                qimage = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
                self.pdf_fallback_label.setPixmap(QPixmap.fromImage(qimage))
                self.pdf_fallback_label.setMinimumSize(qimage.size())
                self.pdf_counter.setText(f'Page {self.pdf_fallback_page+1} / {count}')
                self.pdf_prev.setEnabled(self.pdf_fallback_page > 0)
                self.pdf_next.setEnabled(self.pdf_fallback_page < count-1)
        except Exception as exc:
            self.pdf_fallback_label.setText(f'PDF preview error: {exc}')

    def pdf_move(self, delta):
        if not self.pdf_view or not self.pdf_doc:
            if getattr(self, 'pdf_fallback_path', None):
                self.pdf_fallback_page += delta
                self.pdf_render_fallback()
            return
        navigator = self.pdf_view.pageNavigator()
        page = max(0, min(self.pdf_doc.pageCount()-1, navigator.currentPage()+delta))
        if self.pdf_doc.pageCount():
            navigator.jump(page, navigator.currentLocation(), navigator.currentZoom())
        self.update_pdf_counter()

    def pdf_zoom(self, factor):
        if not self.pdf_view:
            self.pdf_fallback_zoom = max(0.25, min(4.0, self.pdf_fallback_zoom * factor))
            self.pdf_render_fallback()
            return
        if self.pdf_view:
            self.pdf_view.setZoomMode(QPdfView.ZoomMode.Custom)
            self.pdf_view.setZoomFactor(max(0.2, min(5.0, self.pdf_view.zoomFactor()*factor)))

    def office_update_counter(self, *_args):
        if self.office_render_pdf_path:
            return
        count = self.office_pdf_doc.pageCount() if self.office_pdf_doc else 0
        page = self.office_pdf_view.pageNavigator().currentPage() + 1 if count else 0
        self.office_page_info.setText(f'Page {page} / {count}')
        self.office_previous.setEnabled(page > 1)
        self.office_next.setEnabled(page < count)

    def office_move(self, direction):
        if self.office_render_pdf_path:
            self.office_render_page_start = max(0, min(max(0, self.office_render_page_count - 1), self.office_render_page_start + direction))
            self.office_show_raster_pages()
            return
        if not self.office_pdf_doc or not self.office_pdf_view:
            return
        count = self.office_pdf_doc.pageCount()
        if count:
            nav = self.office_pdf_view.pageNavigator()
            page = max(0, min(count-1, nav.currentPage() + direction))
            nav.jump(page, nav.currentLocation(), nav.currentZoom())

    def office_zoom(self, factor):
        if self.office_render_pdf_path:
            self.office_fit_mode = 'width'
            self.office_render_zoom = max(0.4, min(3.0, self.office_render_zoom * factor))
            self.office_show_raster_pages()
            return
        if self.office_pdf_view:
            self.office_pdf_view.setZoomMode(QPdfView.ZoomMode.Custom)
            self.office_pdf_view.setZoomFactor(max(0.2, min(5, self.office_pdf_view.zoomFactor() * factor)))

    def office_fit_pages(self):
        if self.office_render_pdf_path:
            self.office_fit_mode = 'width'
            self.office_render_zoom = 1.0
            self.office_show_raster_pages()
        elif self.office_pdf_view:
            self.office_pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)

    def office_show_raster_pages(self):
        """Rasterize the converted temporary PDF, retaining images and fonts.

        Keep image buffers alive through QImage.copy(), cap memory per batch,
        and report whether a visually blank page is blank in the PDF itself.
        """
        if not self.office_render_pdf_path:
            return
        try:
            import pymupdf as renderer
        except ImportError:
            try:
                import pymupdf as renderer
            except ImportError:
                renderer = None
        if renderer is None and QPdfDocument is None:
            raise RuntimeError('Install pymupdf or PyQt6 QtPdf to display converted Office PDF')
        while self.office_raster_layout.count():
            item = self.office_raster_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        document = None
        try:
            if renderer is not None:
                document = renderer.open(str(self.office_render_pdf_path))
                count = len(document)
            else:
                document = QPdfDocument(self)
                error = document.load(str(self.office_render_pdf_path))
                if error != QPdfDocument.Error.None_:
                    raise RuntimeError(f'QtPdf PDF load error: {error}')
                count = document.pageCount()
            if count < 1:
                raise RuntimeError('Converted PDF has zero pages')
            self.office_render_page_count = count
            first = max(0, min(self.office_render_page_start, count - 1))
            last = min(first + 1, count)
            viewport_width = max(120, self.office_raster_scroll.viewport().width() - 36)
            viewport_height = max(120, self.office_raster_scroll.viewport().height() - 28)
            for index in range(first, last):
                if renderer is not None:
                    page = document[index]
                    scale = min(3.0, max(0.1, viewport_width / max(1, page.rect.width) * self.office_render_zoom))
                    if self.office_fit_mode == 'page':
                        scale = min(scale, viewport_height / max(1, page.rect.height))
                    scale = min(scale, 2400 / max(page.rect.width, page.rect.height))
                    pix = page.get_pixmap(matrix=renderer.Matrix(scale, scale), colorspace=renderer.csRGB, alpha=False, annots=True)
                    image = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
                    # Content statistics distinguish genuinely blank converted PDF
                    # pages from a Qt image-widget display failure.
                    chars = len(page.get_text('text').strip())
                    images = len(page.get_images(full=True))
                    drawings = len(page.get_drawings())
                    info = f'Page {index + 1} / {count} · PDF text: {chars} chars · images: {images} · drawings: {drawings}'
                else:
                    from PyQt6.QtCore import QSize
                    page_size = document.pagePointSize(index)
                    scale = min(3.0, max(0.1, viewport_width / max(1, page_size.width()) * self.office_render_zoom))
                    if self.office_fit_mode == 'page':
                        scale = min(scale, viewport_height / max(1, page_size.height()))
                    scale = min(scale, 2400 / max(page_size.width(), page_size.height()))
                    image = document.render(index, QSize(max(1, int(page_size.width() * scale)), max(1, int(page_size.height() * scale))))
                    info = f'Page {index + 1} / {count}'
                if image.isNull():
                    raise RuntimeError(f'Rasterizer returned empty image for page {index + 1}')
                pixmap = QPixmap.fromImage(image)
                if pixmap.isNull():
                    raise RuntimeError(f'Qt could not display rasterized page {index + 1}')
                # Diagnostics remain available in tooltip, not occupying slide space.
                label = QLabel()
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                label.setPixmap(pixmap)
                label.setToolTip(info)
                label.setMinimumSize(pixmap.size())
                label.setStyleSheet('background: white; border: 1px solid #aaa;')
                self.office_raster_layout.addWidget(label)
            self.office_pages.setCurrentWidget(self.office_raster_scroll)
            self.office_previous.setEnabled(first > 0)
            self.office_next.setEnabled(last < count)
            self.office_page_info.setText(f'Slide {first + 1} / {count}' if getattr(self, 'office_is_presentation', False) else f'Page {first + 1} / {count}')
        finally:
            if document is not None:
                document.close()

    def show_office_rendered(self):
        if self.office_render_pdf_path:
            try:
                self.office_show_raster_pages()
                return
            except Exception as exc:
                self.preview_status.setText(f'Rendered pages unavailable: {exc}')
        self.office_pages.setCurrentWidget(self.office_preview_widget)
        self.preview_status.setText('Rendered pages unavailable; see text fallback.')

    def render_office(self, path: Path):
        """Convert a preservation copy to PDF, then rasterize the PDF pages."""
        from services.office_conversion_service import convert_office
        if QPdfDocument is None:
            import importlib.util
            if not (importlib.util.find_spec('pymupdf') or importlib.util.find_spec('fitz')):
                raise RuntimeError('Install PyMuPDF or PyQt6 QtPdf for rendered pages')
        scratch = Path(self.temp_dir.name) / 'office_render'
        # Release references before replacing the prior temporary conversion.
        self.office_render_pdf_path = None
        if scratch.exists():
            shutil.rmtree(scratch)
        self.office_is_presentation = path.suffix.lower() in {'.ppt', '.pptx', '.pps', '.ppsx', '.odp'}
        pdf = convert_office(path, scratch, 'pdf', '.pdf', timeout=120)
        if pdf.stat().st_size > PREVIEW_LIMIT:
            raise RuntimeError('Rendered PDF exceeds preview size limit')
        self.office_render_pdf_path = pdf
        self.office_render_page_start = 0
        self.office_render_zoom = 1.0
        self.office_fit_mode = 'page'
        try:
            self.office_show_raster_pages()
        except Exception:
            self.office_render_pdf_path = None
            raise
        self._check_office_render_fonts(pdf)
        self._check_office_pdf_content(pdf)

    def _check_office_pdf_content(self, pdf: Path):
        """Report a truly blank converted PDF without blaming the GUI renderer."""
        try:
            import pymupdf
            with pymupdf.open(str(pdf)) as doc:
                sample = [doc[i] for i in range(min(5, len(doc)))]
                counts = [(len(p.get_text('text').strip()), len(p.get_images(full=True)), len(p.get_drawings())) for p in sample]
            if counts and all(not any(c) for c in counts):
                self.preview_status.setText(
                    'LibreOffice generated pages but the first five PDF pages contain no extractable text, images or drawings. '
                    'Check the converted PDF externally and install the fonts used by the document; original evidence is unchanged.'
                )
            elif counts and all(c[0] == 0 for c in counts):
                self.preview_status.setText(
                    'Converted PDF has no extractable text in its first pages. It may contain image-only pages or missing fonts. '
                    'Inspect the page content statistics and compare with the original in LibreOffice.'
                )
        except Exception:
            pass

    def _check_office_render_fonts(self, pdf: Path):
        """Warn about missing Khmer glyphs in converted PDFs, without altering evidence."""
        try:
            import pymupdf
            with pymupdf.open(str(pdf)) as doc:
                sampled = []
                for page in list(doc)[:min(len(doc), 3)]:
                    sampled.append(page.get_text())
                text = '\n'.join(sampled)
            # U+25A1 (white square) and U+FFFD indicate a likely failed font mapping.
            squares = text.count('\u25a1') + text.count('\ufffd')
            if squares >= 5:
                self.preview_status.setText(
                    'Office pages rendered, but the converted PDF contains missing-glyph symbols. '
                    'Install the document fonts (often Khmer OS or Noto Sans Khmer), '
                    'restart LibreOffice, then reopen the file. Original evidence is unchanged.'
                )
        except Exception:
            pass  # Font diagnostics must never block otherwise valid rendering.

    def clear_preview(self):
        if self.player:
            self.player.stop()
            self.player.setSource(QUrl())
        if self.pdf_doc:
            self.pdf_doc.close()
        if self.office_pdf_doc:
            self.office_pdf_doc.close()
        self.office_render_pdf_path = None
        previous_copy = self.preview_path
        self.preview_path = None
        self.selected = None
        if previous_copy is not None:
            try:
                previous_copy.unlink(missing_ok=True)
            except OSError:
                pass
        self.text_preview.clear()
        self.office_preview_widget.clear()
        self.hex_preview.clear()
        self.image_label.clear()
        self.metadata.clear()
        self.preview_status.setText('No file selected')

    def select_file(self):
        """Debounce selection so rapid clicks do not synchronously copy/render every file."""
        selected = self.files.selectedItems()
        if not selected:
            self._preview_timer.stop()
            return
        first = self.files.item(selected[0].row(), 0)
        if first is None:
            return
        self._selection_path = first.data(Qt.ItemDataRole.UserRole)
        self._preview_timer.start()

    def _load_selected_preview(self):
        selected = self.files.selectedItems()
        if not selected:
            return
        first = self.files.item(selected[0].row(), 0)
        if not first:
            return
        path_value = first.data(Qt.ItemDataRole.UserRole)
        if not path_value or path_value != self._selection_path:
            return
        path = Path(path_value)
        self.clear_preview()
        self.selected = path
        try:
            info = path.lstat()
            timestamp = lambda t: datetime.datetime.fromtimestamp(t).astimezone().isoformat(sep=' ', timespec='seconds')
            mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            self.metadata.setPlainText('\n'.join([
                f'Name: {path.name}', f'Source path: {path}',
                f'Type: {"Directory" if path.is_dir() else "File"}',
                f'Extension: {path.suffix or "(none)"}', f'MIME: {mime}',
                f'Size: {info.st_size:,} bytes ({readable_size(info.st_size)})',
                f'Created / metadata changed: {timestamp(info.st_ctime)} (OS-dependent)',
                f'Modified: {timestamp(info.st_mtime)}', f'Accessed: {timestamp(info.st_atime)}',
                f'Permissions: {stat.filemode(info.st_mode)}',
                f'Hidden/system: {hidden(path)}',
                f'Symbolic link: {path.is_symlink()}',
            ]))
            if path.is_symlink():
                self.preview_status.setText('Symlink preview disabled to prevent escaping evidence root.')
                return
            if not path.is_file():
                self.preview_status.setText('Folder selected; double-click to browse.')
                return
            if info.st_size > PREVIEW_LIMIT:
                self.preview_status.setText(f'Preview limited to {readable_size(PREVIEW_LIMIT)}; original remains untouched.')
                return
            self.make_preview_copy(path, info)
        except (OSError, ValueError) as exc:
            self.preview_status.setText(f'Preview unavailable: {exc}')

    def make_preview_copy(self, source, before):
        """Stream source to an isolated temporary copy; decoders never open source."""
        target = Path(self.temp_dir.name) / ('preview_' + hashlib.sha256(os.fsencode(str(source))).hexdigest()[:20] + source.suffix)
        digest = hashlib.sha256()
        try:
            # O_NOFOLLOW prevents following a symlink swapped in after selection.
            flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            fd = os.open(source, flags)
            with os.fdopen(fd, 'rb') as reader, target.open('wb') as writer:
                opened = os.fstat(reader.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise OSError('Source file changed before copying')
                while True:
                    block = reader.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    writer.write(block)
            after = source.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                target.unlink(missing_ok=True)
                self.preview_status.setText('Source changed while copying; preview aborted.')
                return
            self.preview_path = target
            self.metadata.appendPlainText(f'\nPreview copy: {target}\nSHA-256 (copied bytes): {digest.hexdigest()}')
            self.preview_status.setText('Preview loaded from isolated temporary copy (original read-only).')
            self.populate_preview(target)
        except (OSError, PermissionError) as exc:
            target.unlink(missing_ok=True)
            self.preview_status.setText(f'Unable to create preview copy: {exc}')

    def populate_preview(self, path):
        suffix = path.suffix.lower()
        mime = mimetypes.guess_type(path.name)[0] or ''
        with path.open('rb') as file:
            data = file.read(HEX_LIMIT)
        lines = []
        for offset in range(0, len(data), 16):
            chunk = data[offset:offset + 16]
            chars = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
            lines.append(f'{offset:08X}  {chunk.hex(" ").upper():47}  {chars}')
        self.hex_preview.setPlainText('\n'.join(lines) + ('\n… preview truncated' if path.stat().st_size > HEX_LIMIT else ''))
        if suffix in {'.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx'}:
            self.preview_tabs.setCurrentWidget(self.office_layout_container)
            # Prefer a genuine rendered layout, with extracted text as fallback.
            # Text extraction is independent of the LibreOffice installation for OOXML.
            try:
                if suffix in {'.docx', '.pptx', '.xlsx'}:
                    with zipfile.ZipFile(path) as archive:
                        entries = archive.infolist()
                        if len(entries) > 20000 or sum(i.file_size for i in entries) > 512 * 1024 * 1024:
                            raise ValueError('Office package exceeds safe preview limits')
                    extracted = office_preview(path)
                else:
                    extracted = legacy_office_preview(path)
                self.office_preview_widget.setHtml(office_preview_html(extracted))
            except Exception as exc:
                self.office_preview_widget.setPlainText(f'Text extraction unavailable: {exc}')
            try:
                self.render_office(path)
                self.preview_status.setText('Office document rendered from temporary copy using LibreOffice. Layout, images and tables are shown as PDF pages.')
            except Exception as exc:
                self.office_pages.setCurrentWidget(self.office_preview_widget)
                self.office_preview_widget.append('\n\n--- Rendered pages unavailable ---\n' + html.escape(str(exc)))
                self.preview_status.setText(f'Rendered Office view unavailable: {exc}. Showing extracted text where possible.')
        elif mime.startswith('text/') or suffix in {'.json', '.xml', '.csv', '.md', '.py', '.log', '.ini', '.yaml', '.yml', '.sql', '.html', '.js'}:
            with path.open('rb') as f:
                raw = f.read(TEXT_LIMIT)
            self.text_preview.setPlainText(raw.decode('utf-8-sig', errors='replace'))
            self.preview_tabs.setCurrentWidget(self.text_preview)
        elif mime.startswith('image/'):
            from PyQt6.QtGui import QImageReader
            reader = QImageReader(str(path))
            reader.setAutoTransform(True)
            original_size = reader.size()
            if original_size.isValid() and (original_size.width() > 20000 or original_size.height() > 20000):
                self.preview_status.setText('Image dimensions exceed safe preview limits.')
                return
            if original_size.isValid():
                original_size.scale(900, 700, Qt.AspectRatioMode.KeepAspectRatio)
                reader.setScaledSize(original_size)
            pixmap = QPixmap.fromImageReader(reader)
            if not pixmap.isNull():
                self.image_label.setPixmap(pixmap.scaled(900, 700, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                self.preview_tabs.setCurrentIndex(2)
        elif suffix == '.pdf':
            if QPdfDocument and self.pdf_view:
                if self.pdf_doc.load(str(path)) == QPdfDocument.Error.None_:
                    self.preview_view_loaded()
                    self.preview_tabs.setCurrentIndex(4)
                else:
                    self.preview_status.setText('PDF preview decoder could not open the temporary copy.')
            else:
                self.pdf_fallback_path = path
                self.pdf_fallback_page = 0
                self.pdf_fallback_zoom = 1.0
                self.pdf_render_fallback()
                self.preview_tabs.setCurrentIndex(4)
                self.preview_status.setText('PDF preview rendered from temporary copy with PyMuPDF.')
        elif mime.startswith(('video/', 'audio/')):
            if self.player:
                self.player.setSource(QUrl.fromLocalFile(str(path)))
                self.preview_tabs.setCurrentIndex(5)
            else:
                self.preview_status.setText('Media decoder unavailable; Hex preview is still available.')
        else:
            self.preview_tabs.setCurrentWidget(self.hex_preview)

    def start_screen_recording(self):
        if self.recording_worker is not None and self.recording_worker.isRunning():
            return
        default = f"screen_evidence_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
        filename, _ = QFileDialog.getSaveFileName(self, 'Save screen evidence',
            str(Path.home() / 'Documents' / default), 'MP4 Video (*.mp4)')
        if not filename:
            return
        destination = Path(filename)
        if destination.suffix.lower() != '.mp4':
            destination = destination.with_suffix('.mp4')
        if destination.exists():
            QMessageBox.warning(self, 'Recording', 'Choose a new filename. Existing evidence must not be overwritten.')
            return
        # Check optional recording dependencies before starting the worker.
        # Missing packages are not the same as macOS privacy permission errors.
        try:
            from services.screen_recording import check_recording_dependencies
            check_recording_dependencies()
        except (ImportError, RuntimeError) as exc:
            QMessageBox.warning(self, 'Screen recording setup', str(exc))
            self.record_status.setText('Screen recording dependencies unavailable.')
            return
        self.recording_worker = ScreenRecordingWorker(destination, parent=self)
        self.recording_worker.status.connect(self.record_status.setText)
        self.recording_worker.completed.connect(self._recording_completed)
        self.recording_worker.failed.connect(self._recording_failed)
        self.recording_worker.finished.connect(self._recording_finished)
        self.record_start.setEnabled(False)
        self.record_pause.setEnabled(True)
        self.record_stop.setEnabled(True)
        self.record_pause.setText('Pause')
        self.record_status.setText('Starting capture… macOS may request Screen Recording permission.')
        self.recording_worker.start()

    def toggle_screen_pause(self):
        worker = self.recording_worker
        if worker is None or not worker.isRunning():
            return
        paused = self.record_pause.text() == 'Pause'
        worker.set_paused(paused)
        self.record_pause.setText('Resume' if paused else 'Pause')
        self.record_status.setText('PAUSED — resume to continue recording' if paused else 'Recording resumed')

    def stop_screen_recording(self):
        if self.recording_worker is not None:
            self.recording_worker.stop_recording()
            self.record_pause.setEnabled(False)
            self.record_stop.setEnabled(False)
            self.record_status.setText('Finalizing video and calculating SHA-256…')

    def _recording_completed(self, filename, manifest):
        self.record_status.setText(f'Saved: {filename} | SHA-256 manifest: {manifest}')
        QMessageBox.information(self, 'Screen evidence saved',
            f'Video: {filename}\n\nIntegrity manifest: {manifest}')

    def _recording_failed(self, message):
        self.record_status.setText(f'Recording failed: {message}')
        extra = ('\n\nOn macOS, check System Settings → Privacy & Security → '
                 'Screen & System Audio Recording, then restart the app.'
                 if any(term in message.lower() for term in ('permission', 'screen capture', 'not authorized', 'access denied')) else '')
        QMessageBox.warning(self, 'Screen recording failed', message + extra)

    def _recording_finished(self):
        self.record_start.setEnabled(True)
        self.record_pause.setEnabled(False)
        self.record_stop.setEnabled(False)

    def closeEvent(self, event):
        if self.recording_worker is not None and self.recording_worker.isRunning():
            self.recording_worker.stop_recording()
            if not self.recording_worker.wait(60000):
                event.ignore()
                QMessageBox.warning(self, 'Recording still finalizing', 'Wait for recording to finish before closing.')
                return
        self._preview_timer.stop()
        self._search_filter_timer.stop()
        self._page_token += 1
        self._page_worker.shutdown()
        self._page_worker.wait(5000)
        self.stop_search()
        if self.search_worker is not None and self.search_worker.isRunning():
            self.search_worker.wait(5000)
        self.clear_preview()
        if self.search_worker is None or not self.search_worker.isRunning():
            self.temp_dir.cleanup()
        super().closeEvent(event)
