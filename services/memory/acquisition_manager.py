"""Validated physical RAM acquisition facade."""
from services.physical_memory_backend import capture_memory, destination_checks, UnsupportedCapture
from .compatibility_checker import check_compatibility

def acquire(tool, executable, output, **kwargs):
    supported=next((t for t in check_compatibility() if t['name']==tool and t['can_acquire']),None)
    if supported is None:
        raise UnsupportedCapture(f"No compatible, ready acquisition backend: {tool}")
    return capture_memory(tool, executable, output, **kwargs)
