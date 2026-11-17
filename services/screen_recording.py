"""Explicit, visible screen recording for forensic documentation (no audio)."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Event
from PyQt6.QtCore import QThread, pyqtSignal


def check_recording_dependencies():
    """Find screen capture and an FFmpeg encoder without requiring imageio_ffmpeg."""
    import importlib.util
    import shutil
    import sys
    import os

    if importlib.util.find_spec('mss') is None:
        raise RuntimeError(
            'Missing screen capture module: mss.\n\n'
            f'Install using the interpreter running this application:\n'
            f'"{sys.executable}" -m pip install mss'
        )

    # Use the packaged encoder when present, but do not require its Python module.
    if importlib.util.find_spec('imageio_ffmpeg') is not None:
        try:
            import imageio_ffmpeg
            executable = imageio_ffmpeg.get_ffmpeg_exe()
            if executable and os.path.isfile(executable) and os.access(executable, os.X_OK):
                return executable
        except Exception:
            pass

    # PyCharm GUI sessions sometimes have a different PATH from Terminal.
    candidates = [shutil.which('ffmpeg'), '/opt/homebrew/bin/ffmpeg',
                  '/usr/local/bin/ffmpeg', '/opt/local/bin/ffmpeg']
    for executable in candidates:
        if executable and os.path.isfile(executable) and os.access(executable, os.X_OK):
            return executable

    raise RuntimeError(
        'FFmpeg video encoder not found.\n\n'
        'Option 1 (recommended): install FFmpeg using Homebrew:\n'
        'brew install ffmpeg\n\n'
        'Option 2: install the bundled encoder into the EXACT Python running HashTools:\n'
        f'"{sys.executable}" -m pip install imageio-ffmpeg\n\n'
        f'Current Python: {sys.executable}\n'
        'This is an encoder setup issue, not a macOS screen recording permission issue.'
    )


class ScreenRecordingWorker(QThread):
    status = pyqtSignal(str)
    completed = pyqtSignal(str, str)
    failed = pyqtSignal(str)

    def __init__(self, destination: Path, fps: int = 8, parent=None):
        super().__init__(parent)
        self.destination = Path(destination)
        self.fps = fps
        self._stop = Event()
        self._pause = Event()

    def stop_recording(self):
        self._stop.set()

    def set_paused(self, paused: bool):
        if paused:
            self._pause.set()
        else:
            self._pause.clear()

    def run(self):
        proc = None
        encoder_log = None
        started = dt.datetime.now(dt.timezone.utc)
        frames = 0
        try:
            ffmpeg = check_recording_dependencies()
            import mss
            with mss.mss() as capture:
                monitor = capture.monitors[1]  # primary physical screen
                width = int(monitor['width']) // 2 * 2
                height = int(monitor['height']) // 2 * 2
                if width < 2 or height < 2:
                    raise RuntimeError('Invalid screen dimensions')
                self.destination.parent.mkdir(parents=True, exist_ok=True)
                cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-n',
                       '-f', 'rawvideo', '-pixel_format', 'bgra', '-video_size', f'{width}x{height}',
                       '-framerate', str(self.fps), '-i', 'pipe:0', '-an',
                       '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                       '-movflags', '+faststart', str(self.destination)]
                encoder_log = tempfile.TemporaryFile(mode='w+b')
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=encoder_log)
                interval = 1.0 / self.fps
                next_frame = time.monotonic()
                last_status = 0.0
                while not self._stop.is_set():
                    if self._pause.is_set():
                        time.sleep(0.1)
                        next_frame = time.monotonic()
                        continue
                    now = time.monotonic()
                    if now < next_frame:
                        time.sleep(min(next_frame - now, 0.05))
                        continue
                    shot = capture.grab({'left': monitor['left'], 'top': monitor['top'], 'width': width, 'height': height})
                    if proc.poll() is not None:
                        raise RuntimeError('Video encoder terminated unexpectedly')
                    proc.stdin.write(shot.bgra)
                    frames += 1
                    next_frame = max(next_frame + interval, time.monotonic())
                    if now - last_status >= 1:
                        self.status.emit(f'RECORDING • {frames} frames • {self.destination.name}')
                        last_status = now
            if proc and proc.stdin:
                proc.stdin.close()
            if proc:
                code = proc.wait(timeout=45)
                encoder_log.seek(0)
                err = encoder_log.read().decode('utf-8', errors='replace')[-2000:]
                if code != 0:
                    raise RuntimeError(f'Encoder error ({code}): {err}')
            if not frames or not self.destination.is_file() or not self.destination.stat().st_size:
                raise RuntimeError('No video frames were saved')
            digest = hashlib.sha256()
            with self.destination.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            manifest = self.destination.with_suffix('.evidence.json')
            metadata = {
                'file': self.destination.name, 'sha256': digest.hexdigest(),
                'size_bytes': self.destination.stat().st_size, 'frames': frames,
                'fps_target': self.fps, 'started_utc': started.isoformat(),
                'finished_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                'capture': 'primary screen', 'audio': False,
                'note': 'Screen recording of visible desktop; not a native export of source application data.'
            }
            manifest.write_text(json.dumps(metadata, indent=2), encoding='utf-8')
            self.completed.emit(str(self.destination), str(manifest))
        except Exception as exc:
            if proc is not None:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            self.failed.emit(str(exc))
        finally:
            if encoder_log is not None:
                encoder_log.close()
