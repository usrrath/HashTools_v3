from services.memory.evidence_manifest import create_manifest
import pytest
def test_manifest_no_overwrite(tmp_path):
 path=tmp_path/"manifest.json"
 create_manifest(path,"evidence.mem")
 with pytest.raises(FileExistsError): create_manifest(path,"other.mem")
