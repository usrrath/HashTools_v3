"""Responsive, evidence-safe memory capture interface."""
from __future__ import annotations
import json
import threading
import os
import sys
import subprocess
from pathlib import Path
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QLineEdit, QPushButton, QFileDialog, QProgressBar, QPlainTextEdit, QTabWidget,
    QGroupBox, QMessageBox, QComboBox, QCheckBox, QSplitter, QGridLayout)
from datetime import datetime
from services.memory.live_response_collector import collect_response, system_info
from services.memory_hashing import hash_file as sha256_of, format_bytes, format_eta
from services.memory.tool_registry import detect_backends, register_custom_tool
from services.memory.acquisition_manager import acquire as capture_memory
from services.memory.acquisition_verifier import verify as verify_image
from services.physical_memory_backend import (
    IMAGE_SUFFIXES, is_elevated, native_architecture, UnsupportedCapture, sidecar_paths)

class Job(QThread):
    status = pyqtSignal(str)
    progress = pyqtSignal(object)     # structured: phase, percent, rate, eta ...
    result = pyqtSignal(object)
    failed = pyqtSignal(str)
    def __init__(self, kind, *args, **opts):
        super().__init__()
        self.kind, self.args, self.opts = kind, args, opts
        self.stop = threading.Event()
    def run(self):
        try:
            if self.kind == 'response':
                value = collect_response(*self.args, cancelled=self.stop.is_set, progress=self.status.emit)
            elif self.kind == 'physical':
                value = capture_memory(*self.args[:3], cancelled=self.stop.is_set, progress=self.status.emit,
                                       report=self.progress.emit, case_id=self.args[3], examiner=self.args[4],
                                       algorithms=self.opts['algorithms'], verify=self.opts['verify'])
            else:
                self.status.emit('Computing hashes without modifying the image…')
                value = verify_image(self.args[0], self.opts.get('expected'), cancelled=self.stop.is_set,
                                     progress=self.status.emit, algorithms=self.opts['algorithms'],
                                     report=self.progress.emit)
            self.result.emit(value)
        except Exception as exc:
            self.failed.emit(f'{type(exc).__name__}: {exc}')

PHASE_LABEL = {'acquire': 'Acquiring', 'hash': 'Hashing', 'verify': 'Verifying'}

def fresh_evidence_name(physical, folder=None):
    """A new timestamped default name that is not already used in ``folder`` (including sidecars)."""
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    stem, ext = ('memory_dump', '.aff4' if os.name == 'nt' else '.lime') if physical else ('live_response', '.zip')
    candidate, counter = f'{stem}_{stamp}{ext}', 2
    while folder is not None and _name_taken(Path(folder), candidate, physical):
        candidate = f'{stem}_{stamp}_{counter}{ext}'
        counter += 1
    return candidate

def _name_taken(folder, name, physical):
    path = folder / name
    return path.exists() or (physical and any(p.exists() for p in sidecar_paths(path).values()))

class MemoryCaptureTab(QWidget):
    def __init__(self):
        super().__init__()
        self.job = None
        self._outcome = None
        self.setObjectName('memoryCaptureTab')
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)
        title = QLabel('Memory Capture  |  Forensic Evidence Acquisition')
        title.setStyleSheet('font-size: 17px; font-weight: 700;')
        root.addWidget(title)
        description = QLabel('Collect live system artifacts, capture physical RAM with a trusted external tool, or verify an existing memory image.')
        description.setWordWrap(True)
        root.addWidget(description)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 3)
        capture = QWidget()
        capture_layout = QVBoxLayout(capture)
        capture_layout.setContentsMargins(12, 12, 12, 12)
        capture_layout.setSpacing(12)
        group = QGroupBox('Evidence details')
        form = QFormLayout(group)
        form.setSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.case = QLineEdit()
        self.case.setPlaceholderText('e.g. CASE-2026-001')
        form.addRow('Case ID', self.case)
        self.examiner = QLineEdit()
        self.examiner.setPlaceholderText('Examiner name')
        form.addRow('Examiner', self.examiner)
        self.mode = QComboBox()
        self.mode.addItems(['Live-response collection (ZIP)', 'Physical RAM acquisition (AFF4 / LiME)'])
        self.backend = QComboBox()
        self._detected_backends = []
        self.recheck = QPushButton('Re-check tools')
        self.recheck.setToolTip('Look for installed acquisition tools again without restarting HashTools.')
        self.recheck.clicked.connect(self.refresh_backends)
        backend_row = QHBoxLayout()
        backend_row.addWidget(self.backend, 1)
        backend_row.addWidget(self.recheck)
        self.browse_tool = QPushButton('Browse tool…')
        self.browse_tool.setToolTip('Approve a specific acquisition executable (shows its SHA-256 first). '
                                    'Alternatively place it in the "tools" folder next to HashTools or on PATH.')
        self.browse_tool.clicked.connect(self.choose_tool)
        backend_row.addWidget(self.browse_tool)
        form.addRow('Acquisition tool', backend_row)
        self.populate_backends()
        form.addRow('Acquisition method', self.mode)
        self.output_format = QComboBox()
        self.output_format.addItem('AFF4 container (.aff4) — WinPmem mini', '.aff4')
        self.output_format.addItem('Raw memory (.mem) — requires verified raw-capable tool', '.mem')
        self.output_format.addItem('Raw memory (.ram) — requires verified raw-capable tool', '.ram')
        self.output_format.addItem('Raw memory (.raw) — requires verified raw-capable tool', '.raw')
        self.output_format.addItem('LiME image (.lime) — AVML', '.lime')
        self.output_format.setToolTip('An extension does not convert a memory image. WinPmem mini creates AFF4; AVML creates LiME. Raw output requires a separately validated acquisition adapter.')
        self.output_format.currentIndexChanged.connect(self._output_format_changed)
        form.addRow('Physical output format', self.output_format)
        self.folder = QLineEdit(str(Path.home() / 'Documents'))
        browse = QPushButton('Browse…')
        browse.clicked.connect(self.choose_folder)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        form.addRow('Destination folder', folder_row)
        self.filename = QLineEdit(fresh_evidence_name(False))
        form.addRow('Evidence filename', self.filename)
        self.algo_boxes = {}
        algo_row = QHBoxLayout()
        for key, caption in (('md5', 'MD5'), ('sha1', 'SHA-1'), ('sha256', 'SHA-256')):
            box = QCheckBox(caption)
            box.setChecked(True)
            self.algo_boxes[key] = box
            algo_row.addWidget(box)
        algo_row.addStretch(1)
        form.addRow('Hashes', algo_row)
        self.verify_after = QCheckBox('Verify image after capture (re-read the file and confirm SHA-256)')
        self.verify_after.setChecked(True)
        self.verify_after.setToolTip('The image is hashed while it is written. Verification re-reads the finished '
                                     'file to confirm those digests. Turn off only to save time.')
        form.addRow('Integrity', self.verify_after)
        capture_layout.addWidget(group)
        support_group = QGroupBox('Acquisition availability')
        support_layout = QVBoxLayout(support_group)
        self.support = QLabel()
        self.support.setWordWrap(True)
        support_layout.addWidget(self.support)
        self.elevate = QPushButton('Run HashTools as Administrator')
        self.elevate.setToolTip('Windows only: request elevation using the standard UAC prompt. No security controls are bypassed.')
        self.elevate.clicked.connect(self.request_elevation)
        support_layout.addWidget(self.elevate)
        self.switch_live = QPushButton('Switch to available Live-Response ZIP collection')
        self.switch_live.clicked.connect(lambda: self.mode.setCurrentIndex(0))
        support_layout.addWidget(self.switch_live)
        self.pagefile = QLabel('Pagefile capture is not supported. No pagefile data will be included in the ZIP.')
        self.pagefile.setWordWrap(True)
        support_layout.addWidget(self.pagefile)
        capture_layout.addWidget(support_group)
        self.start = QPushButton('Start Live-Response Capture')
        self.start.setMinimumHeight(38)
        self.start.clicked.connect(self.capture)
        self.cancel = QPushButton('Cancel')
        self.cancel.setEnabled(False)
        self.cancel.clicked.connect(self.cancel_job)
        buttons = QHBoxLayout()
        buttons.addWidget(self.start, 3)
        buttons.addWidget(self.cancel, 1)
        capture_layout.addLayout(buttons)
        capture_layout.addStretch(1)
        self.tabs.addTab(capture, 'Live Capture')
        inspect = QWidget()
        inspect_layout = QVBoxLayout(inspect)
        verify_group = QGroupBox('Read-only memory image integrity check')
        verify_form = QFormLayout(verify_group)
        self.image = QLineEdit()
        self.image.setPlaceholderText('Import .aff4, .mem, .ram, .raw, .lime, .dmp or .vmem evidence')
        pick = QPushButton('Select image…')
        pick.clicked.connect(self.choose_image)
        image_row = QHBoxLayout()
        image_row.addWidget(self.image, 1)
        image_row.addWidget(pick)
        verify_form.addRow('Image file', image_row)
        self.expected = QLineEdit()
        self.expected.setPlaceholderText('Optional: expected MD5 / SHA-1 / SHA-256 (.sha256/.md5/.metadata.json beside the image are read automatically)')
        verify_form.addRow('Expected hash', self.expected)
        self.verify = QPushButton('Calculate hashes and compare')
        self.verify.clicked.connect(self.verify_image)
        verify_form.addRow(self.verify)
        inspect_layout.addWidget(verify_group)
        hint = QLabel('Verification reads the selected file only (read-only). It compares against a typed hash and against the sidecar files written at acquisition time; it does not analyze memory contents.')
        hint.setWordWrap(True)
        inspect_layout.addWidget(hint)
        inspect_layout.addStretch(1)
        self.tabs.addTab(inspect, 'Verify Memory Image')
        info = QWidget()
        info_layout = QVBoxLayout(info)
        self.system_grid = QGridLayout()
        self.system_labels = {}
        fields = [('os', 'Operating system'), ('os_version', 'OS version / kernel'),
                  ('architecture', 'Architecture'), ('hostname', 'Hostname'),
                  ('username', 'Current user'), ('total_ram', 'Installed RAM'),
                  ('available_ram', 'Available RAM'), ('uptime_seconds', 'Uptime'),
                  ('elevated', 'Administrator / root')]
        for i, (key, caption) in enumerate(fields):
            label = QLabel(caption)
            label.setStyleSheet('font-weight: 600;')
            value = QLabel('—')
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.system_grid.addWidget(label, i, 0)
            self.system_grid.addWidget(value, i, 1)
            self.system_labels[key] = value
        self.system_grid.setColumnStretch(1, 1)
        info_layout.addLayout(self.system_grid)
        self.system = QPlainTextEdit()
        self.system.setReadOnly(True)
        self.system.setMaximumHeight(160)
        self.system.setPlaceholderText('Detailed system metadata (read-only)')
        info_layout.addWidget(self.system)
        info_layout.addStretch(1)
        refresh = QPushButton('Refresh system information')
        refresh.clicked.connect(self.refresh_system)
        info_layout.addWidget(refresh)
        self.tabs.addTab(info, 'System Information')
        self._last_physical = None
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.backend.currentIndexChanged.connect(self.mode_changed)
        self.mode_changed()
        self.refresh_system()
        output_group = QGroupBox('Acquisition progress and activity log')
        output_layout = QVBoxLayout(output_group)
        self.state = QLabel('Ready to collect evidence')
        output_layout.addWidget(self.state)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        output_layout.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText('Acquisition events, verification hashes and errors will appear here.')
        self.log.setMaximumBlockCount(1000)
        self.log.setMinimumHeight(95)
        self.log.setMaximumHeight(210)
        output_layout.addWidget(self.log)
        root.addWidget(output_group, 0)
        self.mode_changed()

    def populate_backends(self):
        """Re-detect tools and keep the current selection when it is still listed."""
        previous = self.backend.currentData()
        previous_name = previous['name'] if previous else None
        self.backend.blockSignals(True)
        self.backend.clear()
        backends = detect_backends()
        self._detected_backends = backends
        for backend in backends:
            if backend.get('compatible'):
                self.backend.addItem(f"{backend['name']} — {backend['reason']}", backend)
        if not self.backend.count():
            self.backend.addItem('No compatible physical RAM acquisition backend for this host', None)
        # Keep the user's choice if still listed; otherwise prefer a tool that is ready now.
        displayed = [self.backend.itemData(i) for i in range(self.backend.count())]
        target = next((i for i, b in enumerate(displayed) if b and b['name'] == previous_name), None)
        if target is None:
            target = next((i for i, b in enumerate(displayed) if b and b['ready']), 0)
        self.backend.setCurrentIndex(target)
        self.backend.blockSignals(False)

    def refresh_backends(self):
        self.populate_backends()
        self.mode_changed()

    def mode_changed(self, *_):
        physical = self.mode.currentIndex() == 1
        backend = self.backend.currentData()
        ready = bool(backend and backend['ready'])
        self.backend.setEnabled(physical and self.job is None)
        self.recheck.setEnabled(self.job is None)
        self.browse_tool.setEnabled(self.job is None and physical)
        self.switch_live.setVisible(physical and not ready)
        self.elevate.setVisible(physical and os.name == 'nt' and not is_elevated())
        self.elevate.setEnabled(self.job is None)
        self.switch_live.setEnabled(self.job is None)
        allowed = not physical or (ready and is_elevated())
        self.start.setEnabled(self.job is None and allowed)
        self.start.setText('Capture Physical RAM' if physical else 'Start Live-Response Capture')
        if physical:
            if not ready:
                reason = self._physical_unavailable_reason()
            elif not is_elevated():
                reason = 'Restart HashTools as Administrator to capture physical RAM.'
            else:
                reason = 'Ready to start physical RAM acquisition.'
            self.start.setToolTip(reason)
        else:
            self.start.setToolTip('Collect volatile process, network and system information to a ZIP; this is not physical RAM.')
        self.backend.setToolTip('Physical acquisition requires an installed, compatible external tool. Live response does not.')
        if self.job is None and hasattr(self, 'state'):
            if physical and not ready:
                self.state.setText(self._physical_unavailable_reason())
            elif physical and not is_elevated():
                self.state.setText('Administrator privileges required')
            else:
                self.state.setText('Ready to collect evidence')
        if hasattr(self, 'verify_after'):
            self.verify_after.setEnabled(physical and self.job is None)
        # Switching method always proposes a name of the right type, so the extension matches the mode.
        if self._last_physical is not None and physical != self._last_physical:
            if physical:
                self.output_format.blockSignals(True)
                self.output_format.setCurrentIndex(0 if os.name == 'nt' else 4)
                self.output_format.blockSignals(False)
            self.filename.setText(fresh_evidence_name(physical))
        self._last_physical = physical
        if physical:
            elevation = 'Running with administrator/root rights.' if is_elevated() else \
                'NOT running with administrator/root rights: acquisition will be refused until HashTools is restarted elevated.'
            self.support.setText((f"{backend['name']}: {backend['reason']}. " if backend else self._physical_unavailable_reason() + '. ') +
                elevation + ' A memory image exists only after the tool exits successfully and the file is hashed. '
                f'Native OS architecture: {native_architecture()}. Supported: Windows x64 (winpmem_mini_x64.exe), '
                'Linux x86_64 and aarch64 (avml). macOS (Intel and Apple Silicon) and Windows ARM64 have no verified '
                'physical RAM tool in HashTools; use Live-Response ZIP there.')
        else:
            self.support.setText('Live response is available on Windows, Linux and macOS. It collects accessible process, network, session and host metadata into a forensic ZIP. It is NOT a physical RAM image.')

    def _physical_unavailable_reason(self):
        system = __import__('platform').system()
        arch = native_architecture()
        matches = [b for b in self._detected_backends if b['name'] == ('WinPmem' if system == 'Windows' else 'AVML')]
        if system == 'Windows' and arch == 'arm64':
            return 'Windows ARM64 detected. WinPmem x64 kernel driver cannot capture ARM64 RAM. No verified ARM64 backend configured; use Live-Response ZIP.'
        if matches:
            return matches[0]['reason']
        return f'No supported physical RAM acquisition backend for {system}/{arch}; use Live-Response ZIP.'

    def refresh_system(self):
        try:
            info = system_info()
            info['native_os_architecture'] = native_architecture()
            info['acquisition_backends'] = self._detected_backends
            self.system.setPlainText(json.dumps(info, indent=2))
            for key, label in self.system_labels.items():
                value = info.get(key)
                if key in ('total_ram', 'available_ram') and isinstance(value, (int, float)):
                    value = f'{value / (1024 ** 3):.2f} GiB'
                elif key == 'uptime_seconds' and isinstance(value, (int, float)):
                    hours, rem = divmod(int(value), 3600)
                    value = f'{hours} h {rem // 60} min'
                elif key == 'elevated':
                    value = 'Yes' if value else 'No'
                label.setText(str(value) if value is not None else 'Unavailable')
        except Exception as exc:
            self.system.setPlainText(f'System information unavailable: {exc}')
            for label in self.system_labels.values():
                label.setText('Unavailable')

    def algorithms(self):
        return tuple(k for k, box in self.algo_boxes.items() if box.isChecked())

    def choose_tool(self):
        compatible = [b for b in self._detected_backends if b.get('compatible')]
        if not compatible:
            QMessageBox.information(self, 'No compatible tool', self._physical_unavailable_reason())
            return
        name = compatible[0]['name']
        hint = 'winpmem_mini_x64.exe' if name == 'WinPmem' else 'avml'
        selected, _ = QFileDialog.getOpenFileName(self, f'Select {hint}', '', f'{hint} (*{Path(hint).suffix or ""});;All files (*)' if name == 'WinPmem' else 'All files (*)')
        if not selected:
            return
        try:
            digest = sha256_of(selected, ('sha256',))['sha256']
        except OSError as exc:
            QMessageBox.warning(self, 'Cannot read tool', str(exc))
            return
        if QMessageBox.question(self, 'Approve acquisition tool',
                f'Approve this executable for physical RAM acquisition?\n\n{selected}\nSHA-256: {digest}\n\n'
                'Compare the SHA-256 with the value published by the tool vendor. It is recorded in the evidence metadata.') \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            register_custom_tool(name, selected)
        except (UnsupportedCapture, OSError) as exc:
            QMessageBox.warning(self, 'Tool not accepted', str(exc))
            return
        self.log.appendPlainText(f'Approved {name}: {selected}  SHA-256 {digest}')
        self.refresh_backends()

    def choose_folder(self):
        selected = QFileDialog.getExistingDirectory(self, 'Choose evidence destination', self.folder.text())
        if selected:
            self.folder.setText(selected)

    def choose_image(self):
        selected, _ = QFileDialog.getOpenFileName(self, 'Select memory image', '', 'Memory images (*.aff4 *.mem *.ram *.raw *.lime *.dmp *.vmem);;All files (*)')
        if selected:
            self.image.setText(selected)

    def _output_format_changed(self, _index):
        if self.mode.currentIndex() != 1:
            return
        suffix = self.output_format.currentData()
        if suffix:
            current = Path(self.filename.text().strip() or 'memory_dump')
            self.filename.setText(current.stem + suffix)

    def capture(self):
        if self.job is not None:
            return
        physical = self.mode.currentIndex() == 1
        name = self.filename.text().strip()
        extension = (('.aff4',) if os.name == 'nt' else ('.lime', '.raw')) if physical else ('.zip',)
        if physical:
            selected_format = self.output_format.currentData()
            supported_format = '.aff4' if os.name == 'nt' else '.lime'
            if selected_format != supported_format:
                QMessageBox.warning(self, 'Output format unavailable',
                    f'The selected acquisition adapter writes {supported_format}, not {selected_format}. '
                    'Changing the filename extension does not convert the image. '
                    'A separately verified raw-capable acquisition tool is required for .mem/.ram/.raw.')
                return
        if not name or name in ('.', '..') or Path(name).name != name or not name.lower().endswith(extension):
            QMessageBox.warning(self, 'Invalid evidence filename', 'Use a simple filename ending in ' + ', '.join(extension))
            return
        folder = Path(self.folder.text().strip()).expanduser()
        if not folder.is_dir():
            QMessageBox.warning(self, 'Invalid destination', 'Select an existing destination folder.')
            return
        path = folder / name
        if _name_taken(folder, name, physical):
            # Never overwrite: propose a fresh timestamped name so the examiner can simply press Start again.
            fresh = fresh_evidence_name(physical, folder)
            self.filename.setText(fresh)
            QMessageBox.warning(self, 'Name already used',
                f'\'{name}\' already exists in\n{folder}\n\nHashTools never overwrites evidence. '
                f'A new name, \'{fresh}\', has been filled in. Review it and press Start again.')
            return
        if physical:
            # Re-detect at the moment of capture so a tool installed since startup is picked up.
            self.refresh_backends()
            backend = self.backend.currentData()
            if not backend or not backend['ready']:
                QMessageBox.warning(self, 'Backend unavailable', self._physical_unavailable_reason())
                return
            if not is_elevated():
                QMessageBox.warning(self, 'Elevation required', f"{backend['name']} needs {backend['privilege']} rights. Restart HashTools elevated.")
                return
            if QMessageBox.question(self, 'Confirm physical RAM acquisition',
                    'Acquire volatile physical RAM from this authorized host? This operation changes system memory and may load a driver.') != QMessageBox.StandardButton.Yes:
                return
            if not self.algorithms():
                QMessageBox.warning(self, 'No hash selected', 'Select at least one hash algorithm.')
                return
            self.launch('physical', backend['name'], backend['path'], str(path), self.case.text().strip(),
                        self.examiner.text().strip(), algorithms=self.algorithms(), verify=self.verify_after.isChecked())
        else:
            self.launch('response', str(path), self.case.text().strip(), self.examiner.text().strip())

    def verify_image(self):
        path = Path(self.image.text().strip()).expanduser()
        try:
            valid = path.is_file() and path.stat().st_size > 0
        except OSError:
            valid = False
        if not valid:
            QMessageBox.warning(self, 'Invalid memory image', 'Select a readable, nonempty image file.')
            return
        if not self.algorithms():
            QMessageBox.warning(self, 'No hash selected', 'Select at least one hash algorithm.')
            return
        self.launch('hash', str(path), algorithms=self.algorithms(), expected=self.expected.text().strip() or None)

    def launch(self, kind, *args, **opts):
        if self.job is not None:
            return
        self._outcome = None
        self._final_text = ''
        job = Job(kind, *args, **opts)
        self.job = job
        job.status.connect(self.on_status)
        job.progress.connect(self.on_progress)
        job.result.connect(self.success)
        job.failed.connect(self.failure)
        job.finished.connect(self.finished)
        self.progress.setFormat('%p%')
        self.progress.setRange(0, 0)          # indeterminate until the first structured report arrives
        self.state.setText('Working…')
        self.start.setEnabled(False)
        self.verify.setEnabled(False)
        self.cancel.setEnabled(True)
        self.log.appendPlainText('--- New operation ---')
        job.start()

    def on_status(self, message):
        self.state.setText(message)
        # Live counters refresh the status line only; everything else is kept in the log.
        if not message.startswith(('Writing image', 'Hashing:')):
            self.log.appendPlainText(message)

    def on_progress(self, info):
        percent = info.get('percent')
        if percent is None:
            self.progress.setRange(0, 0)
        else:
            if self.progress.maximum() == 0:
                self.progress.setRange(0, 1000)
            self.progress.setValue(int(max(0.0, min(100.0, float(percent))) * 10))
        label = PHASE_LABEL.get(info.get('phase'), 'Working')
        rate = info.get('rate') or 0
        self.progress.setFormat(f"{label} {'%p%' if percent is not None else 'in progress'}  ·  {format_bytes(rate)}/s  ·  ETA {format_eta(info.get('eta'))}")

    def success(self, result):
        self._outcome = 'success'
        self._final_text = 'Completed — evidence saved and hashed' if 'tool' in result else ('Live-response ZIP saved (partial collection; see warnings)' if result.get('partial') else 'Live-response evidence ZIP saved and hashed')
        if 'verification' in result:
            self._final_text = f"Verification result: {result['verification']}"
            if result['verification'] == 'MISMATCH':
                self._outcome = 'failed'
                self._final_text = 'Verification result: MISMATCH — image differs from the recorded hash'
            for c in result.get('comparisons', []):
                self.log.appendPlainText(f"{c['algorithm'].upper()}: {'MATCH' if c['match'] else 'MISMATCH'} (reference: {c['source']})")
            if result.get('size_matches_record') is False:
                self.log.appendPlainText('Image size differs from the size recorded at acquisition.')
        elif result.get('verified'):
            self._final_text = 'Completed — image hashed and verified by re-read'
        elif 'hash_source' in result:
            self._final_text = 'Completed — hashed (not re-read; see warnings)'
        for warning in result.get('warnings', []):
            self.log.appendPlainText('WARNING: ' + warning)
        self.log.appendPlainText('RESULT:\n' + json.dumps(result, indent=2))

    def failure(self, message):
        self._outcome = 'failed'
        self.state.setText('Failed or cancelled — see activity log')
        self.log.appendPlainText('FAILED / CANCELLED: ' + message)

    def finished(self):
        self.progress.setFormat('%p%')
        self.progress.setRange(0, 100)
        self.progress.setValue(100 if self._outcome == 'success' else 0)
        self.cancel.setEnabled(False)
        self.verify.setEnabled(True)
        job = self.job
        self.job = None
        self.mode_changed()
        if job is not None:
            job.deleteLater()
        if self._outcome == 'success':
            self.state.setText(getattr(self, '_final_text', 'Completed — evidence saved and hashed'))
        elif self._outcome == 'failed' and getattr(self, '_final_text', '').startswith('Verification'):
            self.state.setText(self._final_text)
        elif self._outcome == 'failed':
            self.state.setText('Failed / cancelled — see activity log')

    def request_elevation(self):
        if os.name != 'nt' or self.job is not None or is_elevated():
            return
        try:
            import ctypes
            if getattr(sys, 'frozen', False):
                exe, argv = sys.executable, sys.argv[1:]
            else:
                exe = sys.executable
                argv = [str(Path(sys.argv[0]).resolve()), *sys.argv[1:]]
            result = ctypes.windll.shell32.ShellExecuteW(
                None, 'runas', exe, subprocess.list2cmdline(argv), os.getcwd(), 1)
            if result <= 32:
                QMessageBox.warning(self, 'Elevation unavailable',
                    f'Windows did not start an elevated copy (result {result}).')
            else:
                QMessageBox.information(self, 'Elevation requested',
                    'An elevated HashTools instance was requested. Verify Administrator: Yes in its System Information tab.')
        except Exception as exc:
            QMessageBox.warning(self, 'Elevation failed', f'{type(exc).__name__}: {exc}')

    def cancel_job(self):
        if self.job is not None:
            self.job.stop.set()
            self.cancel.setEnabled(False)
            self.state.setText('Cancellation requested…')
            self.log.appendPlainText('Cancellation requested; waiting for worker to stop…')

    def shutdown(self):
        if self.job is not None:
            self.job.stop.set()
            self.job.wait(5000)

    def closeEvent(self, event):
        if self.job is not None and self.job.isRunning():
            self.job.stop.set()
            if not self.job.wait(5000):
                event.ignore()
                QMessageBox.warning(self, 'Operation stopping', 'Please wait for the active forensic operation to stop before closing.')
                return
        super().closeEvent(event)
