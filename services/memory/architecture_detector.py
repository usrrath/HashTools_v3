"""Native and process architecture detection."""
import platform
from services.physical_memory_backend import native_architecture

def detect_architecture():
    return {"native": native_architecture(), "process": platform.machine().lower(), "bits": platform.architecture()[0]}
