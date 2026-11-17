"""Forensic live-response evidence collector. Not a physical RAM acquisition engine."""
from __future__ import annotations
import hashlib, json, os, platform, socket, tempfile, time, zipfile
from datetime import datetime, timezone
from pathlib import Path
import psutil

def utc(): return datetime.now(timezone.utc).isoformat()
def system_info():
    m=psutil.virtual_memory()
    return dict(os=platform.system(), os_version=platform.version(), release=platform.release(), architecture=platform.machine(), hostname=socket.gethostname(), username=os.environ.get('USERNAME') or os.environ.get('USER') or 'unknown', total_ram=m.total, available_ram=m.available, uptime_seconds=int(time.time()-psutil.boot_time()), elevated=(os.geteuid()==0 if hasattr(os,'geteuid') else _windows_admin()))
def _windows_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:return False

def hash_file(path, cancelled=lambda:False):
    md5=hashlib.md5(); sha=hashlib.sha256()
    with open(path,'rb') as f:
        while True:
            if cancelled():raise InterruptedError('Hashing cancelled')
            b=f.read(4*1024*1024)
            if not b:break
            md5.update(b);sha.update(b)
    return {'md5':md5.hexdigest(),'sha256':sha.hexdigest()}

def collect_response(destination, case_id, examiner, cancelled=lambda:False, progress=lambda msg:None):
    dest=Path(destination)
    if dest.exists():raise FileExistsError(str(dest))
    if not dest.parent.is_dir():raise FileNotFoundError(str(dest.parent))
    evidence={'schema_version':'1.0','source_type':'live_response_collection','case_id':case_id,'examiner':examiner,'collected_utc':utc(),'system':system_info(),'sections':{},'errors':[]}
    def check():
        if cancelled():raise InterruptedError('Collection cancelled')
    progress('Collecting process metadata')
    processes=[]
    for p in psutil.process_iter(['pid','ppid','name','username','create_time','exe','cmdline']):
        check()
        try:
            d=p.info.copy();d['cmdline']=' '.join(d.get('cmdline') or [])[:2048];processes.append(d)
        except (psutil.AccessDenied,psutil.NoSuchProcess,psutil.ZombieProcess, OSError):
            continue
    evidence['sections']['processes']={'collected_utc':utc(),'records':processes}
    check();progress('Collecting network connections')
    sockets=[]
    try:
        for c in psutil.net_connections(kind='inet'):
            check();sockets.append(dict(pid=c.pid,fd=c.fd,family=str(c.family),type=str(c.type),local=str(c.laddr),remote=str(c.raddr),status=c.status))
    except (psutil.AccessDenied,PermissionError) as e:evidence['errors'].append('network: '+str(e))
    evidence['sections']['network']={'collected_utc':utc(),'records':sockets}
    check();progress('Collecting sessions and interfaces')
    try:evidence['sections']['sessions']={'collected_utc':utc(),'records':[dict(name=u.name,terminal=u.terminal,host=u.host,started=u.started) for u in psutil.users()]}
    except Exception as e:evidence['errors'].append('sessions: '+str(e))
    try:evidence['sections']['interfaces']={'collected_utc':utc(),'records':{k:[dict(family=str(a.family),address=a.address,netmask=a.netmask) for a in v] for k,v in psutil.net_if_addrs().items()}}
    except Exception as e:evidence['errors'].append('interfaces: '+str(e))
    check();progress('Writing forensic ZIP')
    data=json.dumps(evidence,ensure_ascii=False,indent=2,default=str).encode('utf-8')
    fd,tmp=tempfile.mkstemp(prefix='.memory_capture_',suffix='.tmp',dir=dest.parent);os.close(fd)
    try:
        with zipfile.ZipFile(tmp,'w',compression=zipfile.ZIP_DEFLATED,allowZip64=True) as z:
            z.writestr('live_response.json',data)
            z.writestr('manifest.json',json.dumps({'source_type':'live_response_collection','collected_utc':utc(),'live_response_sha256':hashlib.sha256(data).hexdigest()},indent=2))
        check();hashes=hash_file(tmp,cancelled);check()
        if dest.exists():raise FileExistsError(str(dest))
        # Exclusive creation without overwriting evidence, including on Windows.
        with open(dest, 'xb') as output, open(tmp, 'rb') as source:
            import shutil
            shutil.copyfileobj(source, output, length=4 * 1024 * 1024)
        return {'path':str(dest),'size':dest.stat().st_size,**hashes,'process_count':len(processes),'connection_count':len(sockets)}
    finally:
        try:os.unlink(tmp)
        except FileNotFoundError:pass
