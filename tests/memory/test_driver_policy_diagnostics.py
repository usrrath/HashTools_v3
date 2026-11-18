from pathlib import Path
from services.physical_memory_backend import _tool_failure_details, IMAGE_SUFFIXES


def test_windows_application_control_blocked(tmp_path):
    log = tmp_path / 'acquisition.log'
    log.write_text('Error: StartService(), Cannot start the driver:An Application Control policy has blocked this file.\nImaging failed with error: -8\n')
    message = _tool_failure_details(log, 4294967288)
    assert 'APPLICATION CONTROL BLOCKED' in message
    assert 'changing .aff4 to .mem or .ram' in message
    assert 'Live-Response ZIP' in message
    assert 'signed -8' in message or 'signed 4294967288' in message


def test_memory_import_suffixes():
    assert '.mem' in IMAGE_SUFFIXES
    assert '.ram' in IMAGE_SUFFIXES
    assert '.aff4' in IMAGE_SUFFIXES
