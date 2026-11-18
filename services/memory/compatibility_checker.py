"""Compatibility is determined by the actual installed backend."""
from .tool_registry import list_tools

def check_compatibility():
    return [{**t, "can_acquire":bool(t['ready'] and t['compatible']),"disabled_reason":None if t['ready'] and t['compatible'] else t['reason']} for t in list_tools()]
