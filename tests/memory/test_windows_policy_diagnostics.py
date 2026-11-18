from services.memory.windows_policy_diagnostics import collect_policy_events, export_policy_events
from services.physical_memory_backend import classify_acquisition_failure


def test_policy_classification():
    issue = classify_acquisition_failure('StartService(): An Application Control policy has blocked this file.', -8)
    assert issue['code'] == 'WINDOWS_DRIVER_POLICY_BLOCKED'
    assert issue['retry_same_tool'] is False


def test_unrelated_error_not_misclassified():
    assert classify_acquisition_failure('File not found', -8)['code'] == 'ACQUISITION_TOOL_FAILED'


def test_export_diagnostics_without_overwrite(tmp_path):
    target = tmp_path / 'diagnostics.json'
    export_policy_events(target)
    assert target.exists()
    import pytest
    with pytest.raises(FileExistsError):
        export_policy_events(target)
