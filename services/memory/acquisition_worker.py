"""Qt acquisition worker with explicit cancellation."""
import threading
from PyQt6.QtCore import QThread, pyqtSignal
from .acquisition_manager import acquire

class AcquisitionWorker(QThread):
    progress=pyqtSignal(str)
    detail=pyqtSignal(object)
    result=pyqtSignal(object)
    error=pyqtSignal(str)
    def __init__(self, tool, executable, output, **options):
        super().__init__(); self.tool=tool; self.executable=executable; self.output=output; self.options=options; self._stop=threading.Event()
    def cancel(self): self._stop.set()
    def run(self):
        try: self.result.emit(acquire(self.tool,self.executable,self.output,cancelled=self._stop.is_set,progress=self.progress.emit,report=self.detail.emit,**self.options))
        except Exception as exc: self.error.emit(f'{type(exc).__name__}: {exc}')
