import pytest
from services.memory.volatility_adapter import run_plugin
def test_missing_image(tmp_path):
 with pytest.raises(FileNotFoundError): run_plugin(tmp_path/"missing.raw","windows.pslist.PsList")
