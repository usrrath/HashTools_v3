"""Bundled Volatility 3 source integration; analyzes existing images, never acquires RAM."""
from __future__ import annotations
import importlib.util
import os
import re
import subprocess
import tempfile
import sys
from pathlib import Path

class AnalysisUnavailable(RuntimeError):
    pass

PLUGINS = (
    "windows.info.Info", "windows.pslist.PsList", "windows.pstree.PsTree",
    "windows.cmdline.CmdLine", "windows.netscan.NetScan", "windows.dlllist.DllList",
    "windows.handles.Handles", "windows.malfind.Malfind", "windows.filescan.FileScan",
    "windows.registry.hivelist.HiveList", "linux.pslist.PsList",
    "linux.pstree.PsTree", "linux.bash.Bash", "linux.lsof.Lsof",
    "mac.pslist.PsList", "mac.pstree.PsTree", "mac.lsof.Lsof",
)

def _source_root():
    candidates = [Path(__file__).resolve().parents[2] / "vendor" / "volatility3_src"]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "vendor" / "volatility3_src")
        candidates.append(Path(sys.executable).resolve().parent / "vendor" / "volatility3_src")
    return next((p for p in candidates if (p / "vol.py").is_file() and (p / "volatility3" / "__init__.py").is_file()), None)

def availability():
    root = _source_root()
    if root:
        if importlib.util.find_spec("pefile") is None:
            return False, "Bundled Volatility 3 found; missing Python dependency: pefile>=2024.8.26"
        return True, f"Bundled Volatility 3: {root}"
    if importlib.util.find_spec("volatility3") is not None:
        return True, "Installed Volatility 3 Python package"
    return False, "Volatility 3 source missing (vendor/volatility3_src)"

def is_available():
    return availability()[0]

def command(image, plugin):
    image = Path(image).expanduser().resolve()
    if not image.is_file():
        raise FileNotFoundError(str(image))
    if not re.fullmatch(r"(?:windows|linux|mac)\.[A-Za-z0-9_.]+", plugin):
        raise ValueError("Invalid Volatility 3 plugin identifier")
    ok, reason = availability()
    if not ok:
        raise AnalysisUnavailable(reason)
    root = _source_root()
    if root:
        return [sys.executable, str(root / "vol.py"), "-f", str(image), "-r", "json", plugin]
    return [sys.executable, "-m", "volatility3", "-f", str(image), "-r", "json", plugin]

def plugin_dependencies():
    """Report optional modules without disabling the entire analysis interface."""
    checks = {"pefile": "pefile", "YARA scanning": "yara_x", "Windows credential plugins": "Crypto"}
    return {label: importlib.util.find_spec(module) is not None for label, module in checks.items()}


def run_plugin(image, plugin, timeout=900, cancelled=None):
    """Execute with disk-backed streams to avoid PIPE deadlocks on large output."""
    import time
    cmd = command(image, plugin)
    env = os.environ.copy()
    root = _source_root()
    if root:
        env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    with tempfile.TemporaryFile(mode="w+b") as out, tempfile.TemporaryFile(mode="w+b") as err:
        proc = subprocess.Popen(cmd, stdout=out, stderr=err, env=env, stdin=subprocess.DEVNULL)
        start = time.monotonic()
        try:
            while proc.poll() is None:
                if cancelled is not None and cancelled():
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    raise AnalysisUnavailable("Analysis cancelled by examiner")
                if time.monotonic() - start > timeout:
                    proc.kill()
                    proc.wait()
                    raise AnalysisUnavailable(f"Analysis timed out after {timeout} seconds")
                time.sleep(0.15)
            out.seek(0)
            err.seek(0)
            if proc.returncode:
                details = err.read().decode("utf-8", "replace")[-5000:]
                if not details:
                    details = out.read().decode("utf-8", "replace")[-5000:]
                raise AnalysisUnavailable(details or f"Volatility exited with code {proc.returncode}")
            return out.read().decode("utf-8", "replace")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
