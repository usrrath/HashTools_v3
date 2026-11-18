"""Volatility 3 CLI adapter: no shell execution, bounded output, timeout."""
import importlib.util
import subprocess
import sys
from pathlib import Path

class AnalysisUnavailable(RuntimeError): pass

def is_available(): return importlib.util.find_spec('volatility3') is not None

def run_plugin(image, plugin, timeout=600):
    image=Path(image).resolve()
    if not image.is_file(): raise FileNotFoundError(str(image))
    if not is_available(): raise AnalysisUnavailable('Install volatility3 to analyze memory images')
    if not isinstance(plugin,str) or not plugin.replace('.','').replace('_','').isalnum() or '.' not in plugin:
        raise ValueError('Invalid Volatility plugin name')
    command=[sys.executable,'-m','volatility3','-f',str(image),'-r','json',plugin]
    try:
        result=subprocess.run(command,capture_output=True,text=True,timeout=timeout,check=False)
    except subprocess.TimeoutExpired as exc:
        raise AnalysisUnavailable('Volatility analysis timed out') from exc
    if result.returncode: raise AnalysisUnavailable((result.stderr or result.stdout)[-3000:])
    return result.stdout
