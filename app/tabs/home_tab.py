from __future__ import annotations
from pathlib import Path
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap, QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFileDialog, QFrame, QGridLayout, QListWidget, QMessageBox
from config.settings import LOGO_PATH, ORG_NAME, KH_ORG_NAME
from utils.ui_utils import make_button
from utils.file_utils import human_size, guess_file_type, collect_files


class HomeTab(QWidget):
    file_selected = pyqtSignal(str)      # first file for Metadata/Hex View
    files_selected = pyqtSignal(list)    # all files for Hashing Calculator

    def __init__(self):
        super().__init__()
        self.file_path: str | None = None
        self.files: list[str] = []
        self.setAcceptDrops(True)
        self._build()

    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)

        left = QFrame(); left.setObjectName('card')
        lv = QVBoxLayout(left)
        lv.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        logo = QLabel(); logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if LOGO_PATH.exists():
            logo.setPixmap(QPixmap(str(LOGO_PATH)).scaled(190, 190, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        lv.addWidget(logo)

        kh = QLabel(KH_ORG_NAME); kh.setObjectName('khTitle'); kh.setAlignment(Qt.AlignmentFlag.AlignCenter); lv.addWidget(kh)
        en = QLabel(ORG_NAME); en.setObjectName('enTitle'); en.setAlignment(Qt.AlignmentFlag.AlignCenter); lv.addWidget(en)

        self.btn_files = make_button('📂 Select Files', True); self.btn_files.clicked.connect(self.choose_files); lv.addWidget(self.btn_files)
        self.btn_folder = make_button('📁 Select Folder'); self.btn_folder.clicked.connect(self.choose_folder); lv.addWidget(self.btn_folder)

        drop = QLabel('Drag & Drop multiple files or folders here')
        drop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop.setMinimumHeight(85)
        drop.setStyleSheet('border:2px dashed #93c5fd; border-radius:12px; color:#2563eb; background:#eff6ff; font-weight:700;')
        lv.addWidget(drop)

        self.info = QLabel('Selected Files: 0\nTotal Size: -\nPrimary File: -\nPrimary Type: -')
        self.info.setObjectName('infoBox'); self.info.setWordWrap(True); lv.addWidget(self.info)
        root.addWidget(left, 1)

        main = QFrame(); main.setObjectName('card')
        g = QGridLayout(main)
        title = QLabel('Integrated Analysis Overview'); title.setObjectName('sectionTitle'); g.addWidget(title, 0, 0, 1, 4)
        for i, (icon, name, desc) in enumerate([
            ('#', 'Hashing', 'Calculate hashes for files/folders'),
            ('▣', 'MetaData', 'Extract primary file metadata'),
            ('01', 'Hex View', 'Open primary file in hex viewer'),
            ('📁', 'Batch Upload', 'Support multiple files and folders'),
        ]):
            c = QFrame(); c.setObjectName('moduleCard')
            v = QVBoxLayout(c)
            a = QLabel(icon); a.setObjectName('moduleIcon'); a.setAlignment(Qt.AlignmentFlag.AlignCenter)
            b = QLabel(name); b.setObjectName('moduleTitle'); b.setAlignment(Qt.AlignmentFlag.AlignCenter)
            d = QLabel(desc); d.setAlignment(Qt.AlignmentFlag.AlignCenter); d.setWordWrap(True)
            v.addWidget(a); v.addWidget(b); v.addWidget(d)
            g.addWidget(c, 1, i)

        self.list_widget = QListWidget()
        self.list_widget.setAlternatingRowColors(True)
        g.addWidget(QLabel('Selected Files / Folder Contents'), 2, 0, 1, 4)
        g.addWidget(self.list_widget, 3, 0, 1, 4)
        self.summary = QLabel('Load files or a folder to begin analysis. The first file opens in Metadata and Hex View; all files are sent to Hashing Calculator.')
        self.summary.setObjectName('summaryBox'); self.summary.setWordWrap(True); g.addWidget(self.summary, 4, 0, 1, 4)
        root.addWidget(main, 3)

    def choose_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, 'Select files', str(Path.home()), 'All Files (*.*)')
        if files:
            self.files_selected.emit(files)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, 'Select folder', str(Path.home()))
        if folder:
            self.files_selected.emit([folder])

    def set_file(self, path: str):
        # Preserve backward compatibility for single-file calls.
        self.set_files([path], emit_signals=False)

    def set_files(self, paths: list[str], emit_signals: bool = True, scanned: bool = False):
        files = list(paths) if scanned else collect_files(paths)
        if not files:
            QMessageBox.warning(self, 'No files found', 'No readable files were found in the selected path.')
            return
        self.files = files
        self.file_path = files[0]
        self._refresh_info()
        if emit_signals:
            self.files_selected.emit(files)
            self.file_selected.emit(files[0])

    def _refresh_info(self):
        total = sum(Path(f).stat().st_size for f in self.files if Path(f).exists())
        primary = Path(self.file_path) if self.file_path else None
        self.info.setText(
            f'Selected Files: {len(self.files)}\n'
            f'Total Size: {human_size(total)}\n'
            f'Primary File: {primary.name if primary else "-"}\n'
            f'Primary Type: {guess_file_type(str(primary)) if primary else "-"}'
        )
        self.list_widget.clear()
        for f in self.files[:1000]:
            p = Path(f)
            self.list_widget.addItem(f'{p.name}  |  {human_size(p.stat().st_size)}  |  {p.parent}')
        if len(self.files) > 1000:
            self.list_widget.addItem(f'... {len(self.files) - 1000} more files')
        self.summary.setText(f'Loaded {len(self.files)} file(s). Hashing Calculator is ready for batch hashing. Primary file: {primary.name if primary else "-"}.')

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
        if paths:
            self.files_selected.emit(paths)
            event.acceptProposedAction()
