"""Approved external acquisition tool discovery."""
from services.physical_memory_backend import detect_backends, register_custom_tool, clear_custom_tools

def list_tools():
    return detect_backends()

def available_tools():
    return [tool for tool in list_tools() if tool['ready'] and tool['compatible']]
