"""Memory image analysis UI; optional Volatility 3 integration."""
from pathlib import Path
import json
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,QPushButton,
    QFileDialog,QComboBox,QPlainTextEdit,QMessageBox)
from services.memory.volatility_adapter import is_available
from services.memory.memory_analysis_manager import analyze

PLUGINS = ('windows.pslist.PsList','windows.pstree.PsTree','windows.cmdline.CmdLine',
           'windows.netscan.NetScan','linux.pslist.PsList','linux.pstree.PsTree',
           'mac.pslist.PsList')

class AnalysisJob(QThread):
    result=pyqtSignal(object)
    error=pyqtSignal(str)
    def __init__(self,image,plugin):
        super().__init__(); self.image=image; self.plugin=plugin
    def run(self):
        try: self.result.emit(analyze(self.image,self.plugin))
        except Exception as exc: self.error.emit(f'{type(exc).__name__}: {exc}')

class MemoryAnalysisTab(QWidget):
    def __init__(self):
        super().__init__(); self.job=None; self.last_result=None
        layout=QVBoxLayout(self)
        layout.addWidget(QLabel('Memory Analysis — Volatility 3 (compatible images only)'))
        row=QHBoxLayout(); self.image=QLineEdit(); self.image.setPlaceholderText('Select an existing memory image')
        browse=QPushButton('Browse image…'); browse.clicked.connect(self.browse)
        row.addWidget(self.image,1); row.addWidget(browse); layout.addLayout(row)
        row=QHBoxLayout(); self.plugin=QComboBox(); self.plugin.setEditable(True); self.plugin.addItems(PLUGINS)
        self.run_button=QPushButton('Analyze'); self.run_button.clicked.connect(self.start)
        self.export_button=QPushButton('Export JSON…'); self.export_button.clicked.connect(self.export); self.export_button.setEnabled(False)
        row.addWidget(self.plugin,1); row.addWidget(self.run_button); row.addWidget(self.export_button); layout.addLayout(row)
        self.status=QLabel('Volatility 3 available' if is_available() else 'Volatility 3 not installed — analysis requires optional volatility3 dependency')
        self.status.setWordWrap(True); layout.addWidget(self.status)
        self.output=QPlainTextEdit(); self.output.setReadOnly(True); layout.addWidget(self.output,1)
    def browse(self):
        path,_=QFileDialog.getOpenFileName(self,'Select memory image','','Memory images (*.raw *.mem *.lime *.dmp *.vmem);;All files (*)')
        if path: self.image.setText(path)
    def start(self):
        path=Path(self.image.text()).expanduser()
        if not path.is_file(): QMessageBox.warning(self,'Missing evidence','Choose an existing memory image.'); return
        if not is_available(): QMessageBox.warning(self,'Missing dependency','Install the optional volatility3 package.'); return
        if self.job and self.job.isRunning(): return
        self.last_result=None; self.export_button.setEnabled(False); self.run_button.setEnabled(False)
        self.status.setText('Analyzing image…'); self.job=AnalysisJob(str(path),self.plugin.currentText().strip())
        self.job.result.connect(self.completed); self.job.error.connect(self.failed)
        self.job.finished.connect(lambda: self.run_button.setEnabled(True)); self.job.start()
    def completed(self,result):
        self.last_result=result; self.output.setPlainText(json.dumps(result,indent=2,default=str)[:1000000]); self.export_button.setEnabled(True); self.status.setText('Analysis finished')
    def failed(self,error): self.status.setText(error); self.output.setPlainText(error)
    def export(self):
        if self.last_result is None: return
        path,_=QFileDialog.getSaveFileName(self,'Export analysis','','JSON (*.json)')
        if not path: return
        try:
            from services.memory.artifact_exporter import export_json
            export_json(path,self.last_result); self.status.setText(f'Exported: {path}')
        except Exception as exc: QMessageBox.warning(self,'Export failed',str(exc))
    def shutdown(self):
        if self.job and self.job.isRunning():
            self.job.wait(2000)
