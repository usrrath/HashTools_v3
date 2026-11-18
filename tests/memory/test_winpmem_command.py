from pathlib import Path
from services.physical_memory_backend import TOOLS


def test_winpmem_aff4_output_uses_explicit_output_option():
    args = TOOLS['WinPmem']['args']('winpmem_mini_x64.exe', 'C:/Evidence/capture.aff4')
    assert args == ['winpmem_mini_x64.exe', '-o', 'C:/Evidence/capture.aff4']


def test_avml_still_uses_positional_output():
    assert TOOLS['AVML']['args']('avml', '/evidence/ram.lime') == ['avml', '/evidence/ram.lime']
