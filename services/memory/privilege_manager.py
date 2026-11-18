"""Privilege diagnostics (never bypasses platform security)."""
from services.physical_memory_backend import is_elevated

def privilege_status():
    return {"elevated": bool(is_elevated()), "required_for_physical_capture": True}
