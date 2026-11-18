"""Cross-platform, best-effort volatile live-response collector (NOT a RAM image)."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import psutil


def utc():
    return datetime.now(timezone.utc).isoformat()


def _windows_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def system_info():
    mem = psutil.virtual_memory()
    return dict(os=platform.system(), os_version=platform.version(), release=platform.release(),
                architecture=platform.machine(), hostname=socket.gethostname(),
                username=os.environ.get('USERNAME') or os.environ.get('USER') or 'unknown',
                total_ram=mem.total, available_ram=mem.available,
                uptime_seconds=max(0, int(time.time() - psutil.boot_time())),
                elevated=(os.geteuid() == 0 if hasattr(os, 'geteuid') else _windows_admin()))


def hash_file(path, cancelled=lambda: False):
    md5, sha = hashlib.md5(), hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            if cancelled():
                raise InterruptedError('Hashing cancelled')
            data = stream.read(4 * 1024 * 1024)
            if not data:
                break
            md5.update(data)
            sha.update(data)
    return {'md5': md5.hexdigest(), 'sha256': sha.hexdigest()}


def collect_response(destination, case_id, examiner, cancelled=lambda: False, progress=lambda msg: None):
    """Write a self-describing ZIP with individually hashed artifacts; never overwrite evidence."""
    dest = Path(destination).expanduser().absolute()
    if dest.suffix.lower() != '.zip':
        raise ValueError('Live-response evidence filename must end in .zip')
    if dest.exists():
        raise FileExistsError(str(dest))
    if not dest.parent.is_dir():
        raise FileNotFoundError(str(dest.parent))
    if not os.access(dest.parent, os.W_OK):
        raise PermissionError(f'Destination is not writable: {dest.parent}')

    def check():
        if cancelled():
            raise InterruptedError('Live-response collection cancelled')

    errors = []
    sections = {}

    def collect(name, func):
        check()
        progress(f'Collecting {name}')
        try:
            sections[name] = {'collected_utc': utc(), 'records': func(), 'status': 'collected'}
        except (psutil.Error, OSError, PermissionError, RuntimeError, ValueError) as exc:
            errors.append({'section': name, 'error': f'{type(exc).__name__}: {exc}'})
            sections[name] = {'collected_utc': utc(), 'records': None, 'status': 'failed'}

    def processes():
        rows = []
        for proc in psutil.process_iter(['pid', 'ppid', 'name', 'username', 'create_time', 'exe', 'cmdline', 'status']):
            check()
            try:
                record = dict(proc.info)
                record['cmdline'] = ' '.join(record.get('cmdline') or [])[:8192]
                rows.append(record)
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
                continue
        return rows

    def network():
        rows = []
        for conn in psutil.net_connections(kind='inet'):
            check()
            rows.append(dict(pid=conn.pid, fd=conn.fd, family=str(conn.family),
                             type=str(conn.type), local=str(conn.laddr),
                             remote=str(conn.raddr), status=conn.status))
        return rows

    collect('processes', processes)
    collect('network', network)
    collect('sessions', lambda: [dict(name=u.name, terminal=u.terminal, host=u.host,
                                      started=u.started) for u in psutil.users()])
    collect('interfaces', lambda: {name: [dict(family=str(a.family), address=a.address,
                                                netmask=a.netmask) for a in addrs]
                                    for name, addrs in psutil.net_if_addrs().items()})
    collect('interface_status', lambda: {name: {'isup': item.isup, 'speed_mbps': item.speed, 'mtu': item.mtu}
                                         for name, item in psutil.net_if_stats().items()})
    check()
    evidence = {'schema_version': '2.0', 'source_type': 'live_response_collection',
                'case_id': case_id, 'examiner': examiner, 'collected_utc': utc(),
                'system': system_info(), 'sections': sections, 'errors': errors,
                'warning': 'Live response contains volatile metadata, not physical RAM.'}
    data = json.dumps(evidence, ensure_ascii=False, indent=2, default=str).encode('utf-8')
    manifest = {'schema_version': 2, 'source_type': 'live_response_collection',
                'created_utc': utc(), 'artifacts': {
                    'live_response.json': {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}},
                'section_status': {k: v['status'] for k, v in sections.items()},
                'partial': bool(errors)}
    progress('Writing forensic ZIP')
    fd, temp_name = tempfile.mkstemp(prefix='.memory_capture_', suffix='.tmp', dir=dest.parent)
    os.close(fd)
    created = False
    try:
        with zipfile.ZipFile(temp_name, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            archive.writestr('live_response.json', data)
            archive.writestr('manifest.json', json.dumps(manifest, indent=2).encode('utf-8'))
        check()
        # Exclusive creation prevents concurrent acquisitions from overwriting one another.
        with open(dest, 'xb') as output:
            created = True
            with open(temp_name, 'rb') as source:
                while True:
                    check()
                    chunk = source.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
        hashes = hash_file(dest, cancelled)
        progress('Live-response ZIP written and hashed')
        return {'path': str(dest), 'size': dest.stat().st_size, **hashes,
                'process_count': len(sections['processes']['records'] or []),
                'connection_count': len(sections['network']['records'] or []),
                'warnings': [f'{len(errors)} section(s) could not be collected'] if errors else [],
                'partial': bool(errors), 'acquisition_type': 'Live-response metadata ZIP'}
    except BaseException:
        if created:
            dest.unlink(missing_ok=True)  # incomplete copy, not usable evidence
        raise
    finally:
        Path(temp_name).unlink(missing_ok=True)
