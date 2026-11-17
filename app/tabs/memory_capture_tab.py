"""Responsive, evidence-safe memory capture interface."""
from __future__ import annotations
import json
import threading
from pathlib import Path
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QLineEdit, QPushButton, QFileDialog, QProgressBar, QPlainTextEdit, QTabWidget,
    QGroupBox, QMessageBox, QComboBox, QCheckBox, QSplitter, QGridLayout)
from services.memory_capture_service import collect_response, hash_file, system_info
from services.physical_memory_backend import detect_backends, capture_memory, IMAGE_SUFFIXES, is_elevated, native_architecture, request_administrator_restart

class Job(QThread):
    status = pyqtSignal(str)
    result = pyqtSignal(object)
    failed = pyqtSignal(str)
    def __init__(self, kind, *args):
        super().__init__()
        self.kind, self.args = kind, args
        self.stop = threading.Event()
    def run(self):
        try:
            if self.kind == 'response':
                value = collect_response(*self.args, cancelled=self.stop.is_set, progress=self.status.emit)
            elif self.kind == 'physical':
                value = capture_memory(*self.args[:3], cancelled=self.stop.is_set, progress=self.status.emit, case_id=self.args[3], examiner=self.args[4])
            else:
                self.status.emit('Computing MD5 and SHA-256 without modifying the image…')
                value = {'path': self.args[0], **hash_file(self.args[0], self.stop.is_set)}
            self.result.emit(value)
        except Exception as exc:
            self.failed.emit(f'{type(exc).__name__}: {exc}')

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
        self.mode.addItems(['Live-response collection (ZIP)', 'Physical RAM acquisition (.mem)'])
        self.backend = QComboBox()
        self._detected_backends = []
        self.recheck = QPushButton('Re-check tools')
        self.recheck.setToolTip('Look for installed acquisition tools again without restarting HashTools.')
        self.recheck.clicked.connect(self.refresh_backends)
        self.elevate = QPushButton('Restart as Administrator')
        self.elevate.clicked.connect(self.restart_elevated)
        backend_row = QHBoxLayout()
        backend_row.addWidget(self.backend, 1)
        backend_row.addWidget(self.recheck)
        backend_row.addWidget(self.elevate)
        form.addRow('Acquisition tool', backend_row)
        self.populate_backends()
        form.addRow('Acquisition method', self.mode)
        self.folder = QLineEdit(str(Path.home() / 'Documents'))
        browse = QPushButton('Browse…')
        browse.clicked.connect(self.choose_folder)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        form.addRow('Destination folder', folder_row)
        self.filename = QLineEdit('live_response.zip')
        form.addRow('Evidence filename', self.filename)
        capture_layout.addWidget(group)
        support_group = QGroupBox('Acquisition availability')
        support_layout = QVBoxLayout(support_group)
        self.support = QLabel()
        self.support.setWordWrap(True)
        support_layout.addWidget(self.support)
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
        self.image.setPlaceholderText('Choose a .mem, .raw, .lime, .dmp or .vmem file')
        pick = QPushButton('Select image…')
        pick.clicked.connect(self.choose_image)
        image_row = QHBoxLayout()
        image_row.addWidget(self.image, 1)
        image_row.addWidget(pick)
        verify_form.addRow('Image file', image_row)
        self.verify = QPushButton('Calculate MD5 + SHA-256')
        self.verify.clicked.connect(self.verify_image)
        verify_form.addRow(self.verify)
        inspect_layout.addWidget(verify_group)
        hint = QLabel('Verification reads the selected file only. It does not analyze memory contents or determine the source operating system.')
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
        self.elevate.setVisible(physical and not is_elevated())
        self.elevate.setEnabled(self.job is None)
        self.switch_live.setVisible(physical and not ready)
        self.switch_live.setEnabled(self.job is None)
        allowed = not physical or (ready and is_elevated())
        self.start.setEnabled(self.job is None and allowed)
        self.start.setText('Capture Physical RAM' if physical else 'Start Live-Response Capture')
        if physical:
            if not ready:
                reason = self._physical_unavailable_reason()
            elif not is_elevated():
                reason = 'Click Restart as Administrator and accept Windows UAC; your current session will close.'
            else:
                reason = 'Ready to start physical RAM acquisition.'
            self.start.setToolTip(reason)
        else:
            self.start.setToolTip('Collect volatile process, network and system information to a ZIP; this is not physical RAM.')
        self.backend.setToolTip('Physical acquisition requires an installed, compatible external tool. Live response does not.')
        if self.job is None:
            self.state.setText(self._physical_unavailable_reason() if physical and not ready else ('Administrator privileges required' if physical and not is_elevated() else 'Ready to collect evidence')) if hasattr(self, 'state') else None
        if physical and self.filename.text().endswith('.zip'):
            self.filename.setText('memory_dump.mem')
        elif not physical and self.filename.text().endswith(IMAGE_SUFFIXES):
            self.filename.setText('live_response.zip')
        if physical:
            elevation = 'Running with administrator/root rights.' if is_elevated() else \
                'NOT running with administrator/root rights: acquisition will be refused until HashTools is restarted elevated.'
            self.support.setText((f"{backend['name']}: {backend['reason']}. " if backend else self._physical_unavailable_reason() + '. ') +
                elevation + ' A memory image exists only after the tool exits successfully and the file is hashed. '
                f'Native OS architecture: {native_architecture()}. Potential backends: Windows x64 (WinPmem), Linux x64 (AVML). '
                'macOS physical RAM acquisition is unavailable without a separately verified compatible backend.')
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

    def restart_elevated(self):
        if self.job is not None:
            QMessageBox.warning(self, 'Acquisition active', 'Finish or cancel the current operation before restarting.')
            return
        if is_elevated():
            self.refresh_backends()
            return
        answer = QMessageBox.question(
            self, 'Restart as Administrator',
            'Windows will display a UAC permission prompt. The current HashTools session '
            'will close after the elevated instance starts. Unsaved form values will be lost. Continue?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            if request_administrator_restart():
                from PyQt6.QtWidgets import QApplication
                QApplication.instance().quit()
        except Exception as exc:
            QMessageBox.warning(self, 'Elevation failed', str(exc))

    def choose_folder(self):
        selected = QFileDialog.getExistingDirectory(self, 'Choose evidence destination', self.folder.text())
        if selected:
            self.folder.setText(selected)

    def choose_image(self):
        selected, _ = QFileDialog.getOpenFileName(self, 'Select memory image', '', 'Memory images (*.mem *.raw *.lime *.dmp *.vmem);;All files (*)')
        if selected:
            self.image.setText(selected)

    def capture(self):
        if self.job is not None:
            return
        physical = self.mode.currentIndex() == 1
        name = self.filename.text().strip()
        extension = IMAGE_SUFFIXES if physical else ('.zip',)
        if not name or name in ('.', '..') or Path(name).name != name or not name.lower().endswith(extension):
            QMessageBox.warning(self, 'Invalid evidence filename', 'Use a simple filename ending in ' + ', '.join(extension))
            return
        folder = Path(self.folder.text().strip()).expanduser()
        if not folder.is_dir():
            QMessageBox.warning(self, 'Invalid destination', 'Select an existing destination folder.')
            return
        path = folder / name
        if path.exists():
            QMessageBox.warning(self, 'Evidence already exists', 'Existing evidence will never be overwritten.')
            return
        if physical:
            # Re-detect at the moment of capture so a tool installed since startup is picked up.
            self.refresh_backends()
            backend = self.backend.currentData()
            if not backend or not backend['ready']:
                QMessageBox.warning(self, 'Backend unavailable', self._physical_unavailable_reason())
                return
            if not is_elevated():
                QMessageBox.warning(self, 'Elevation required', f"{backend['name']} needs {backend['privilege']} rights. Use Restart as Administrator.")
                return
            if QMessageBox.question(self, 'Confirm physical RAM acquisition',
                    'Acquire volatile physical RAM from this authorized host? This operation changes system memory and may load a driver.') != QMessageBox.StandardButton.Yes:
                return
            self.launch('physical', backend['name'], backend['path'], str(path), self.case.text().strip(), self.examiner.text().strip())
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
        self.launch('hash', str(path))

    def launch(self, kind, *args):
        if self.job is not None:
            return
        self._outcome = None
        job = Job(kind, *args)
        self.job = job
        job.status.connect(self.on_status)
        job.result.connect(self.success)
        job.failed.connect(self.failure)
        job.finished.connect(self.finished)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setRange(0, 0)
        self.state.setText('Working…')
        self.start.setEnabled(False)
        self.verify.setEnabled(False)
        self.cancel.setEnabled(True)
        self.log.appendPlainText('--- New operation ---')
        job.start()

    def on_status(self, message):
        self.state.setText(message)
        self.log.appendPlainText(message)

    def success(self, result):
        self._outcome = 'success'
        self.state.setText('Completed — SHA-256 recorded')
        for warning in result.get('warnings', []):
            self.log.appendPlainText('WARNING: ' + warning)
        self.log.appendPlainText('SUCCESS:\n' + json.dumps(result, indent=2))

    def failure(self, message):
        self._outcome = 'failed'
        self.state.setText('Failed or cancelled — see activity log')
        self.log.appendPlainText('FAILED / CANCELLED: ' + message)

    def finished(self):
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
            self.state.setText('Completed — evidence saved and hashed')
        elif self._outcome == 'failed':
            self.state.setText('Failed / cancelled — see activity log')

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
