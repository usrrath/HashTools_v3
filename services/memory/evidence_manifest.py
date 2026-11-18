"""Atomic, non-overwriting forensic manifest writer."""
import json
import os
from datetime import datetime,timezone
from pathlib import Path

def create_manifest(destination, evidence, metadata=None):
    dest=Path(destination)
    payload={"schema":"hashtools.memory.evidence.v1","created_utc":datetime.now(timezone.utc).isoformat(),"evidence":str(evidence),"metadata":metadata or {}}
    with dest.open('x',encoding='utf-8') as handle:
        json.dump(payload,handle,indent=2,default=str); handle.flush(); os.fsync(handle.fileno())
    return dest
