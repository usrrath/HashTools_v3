"""Append-only custody event records; no invented transfers."""
import json
import os
from datetime import datetime,timezone
from pathlib import Path

def append_event(path, event, actor, details=None):
    if not event or not actor: raise ValueError('Event and actor are required')
    record={"utc":datetime.now(timezone.utc).isoformat(),"event":event,"actor":actor,"details":details or {}}
    with Path(path).open('a',encoding='utf-8') as stream:
        stream.write(json.dumps(record,sort_keys=True,default=str)+'\n'); stream.flush(); os.fsync(stream.fileno())
    return record
