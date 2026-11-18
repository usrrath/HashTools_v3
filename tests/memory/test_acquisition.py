import pytest
from services.memory.acquisition_manager import acquire
def test_invalid_tool(tmp_path):
 with pytest.raises(Exception): acquire("invalid-tool", "none", tmp_path/"ram.mem")
