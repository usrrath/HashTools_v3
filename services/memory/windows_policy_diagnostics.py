"""Read-only Windows acquisition-policy diagnostics. No policy changes are made."""
from __future__ import annotations

import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

LOGS = (
    'Microsoft-Windows-CodeIntegrity/Operational',
    'Microsoft-Windows-AppLocker/EXE and DLL',
)


def collect_policy_events(minutes: int = 30, timeout: int = 15) -> dict:
    """Query recent policy events with native wevtutil; returns errors, never raises for missing logs."""
    if platform.system() != 'Windows':
        return {'status': 'unsupported', 'reason': 'Windows event logs require Windows', 'logs': {}}
    minutes = max(1, min(int(minutes), 1440))
    result = {'status': 'completed', 'collected_utc': datetime.now(timezone.utc).isoformat(), 'logs': {}}
    for name in LOGS:
        # XPath expression is passed as one argument; no shell or PowerShell interpolation.
        xpath = f'*[System[TimeCreated[timediff(@SystemTime) <= {minutes * 60000}]]]'
        try:
            proc = subprocess.run(['wevtutil', 'qe', name, '/f:xml', '/rd:true', '/c:40', f'/q:{xpath}'],
                                  capture_output=True, text=True, errors='replace',
                                  timeout=timeout, shell=False)
            result['logs'][name] = {'ok': proc.returncode == 0,
                                    'xml': proc.stdout[:500000] if proc.returncode == 0 else '',
                                    'error': proc.stderr[:2000] if proc.returncode else ''}
        except (OSError, subprocess.TimeoutExpired) as exc:
            result['logs'][name] = {'ok': False, 'xml': '', 'error': str(exc)}
    return result


def export_policy_events(destination: str | Path, minutes: int = 30) -> Path:
    target = Path(destination).expanduser().absolute()
    if target.exists():
        raise FileExistsError(target)
    result = collect_policy_events(minutes)
    with target.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    return target
