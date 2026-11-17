"""External-tool physical RAM acquisition for Windows x64, Linux x64 and macOS Intel.

Only a user-installed, trusted executable discovered on PATH is used. No drivers or
acquisition binaries are bundled. An acquisition is reported as successful only when
the tool exits with status 0 and a nonempty image exists; the image is then hashed
read-only. Partial images and tool logs are kept on failure or cancellation and are
never marked as validated.
"""
from __future__ import annotations

import hashlib
import ctypes
import json
from datetime import datetime, timezone
import os
import platform
import sys
import shutil
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil

from config import settings

GiB = 1024 ** 3
IMAGE_SUFFIXES = ('.mem', '.raw', '.lime')
# An image far smaller than installed RAM usually means an incomplete acquisition.
MIN_EXPECTED_FRACTION = 0.5
_SIGKILL = getattr(signal, 'SIGKILL', signal.SIGTERM)
# Read window for image hashing. Measured on single-CPU hardware: 1-8 MiB are equivalent
# and 16 MiB is ~9% slower, so the BLAKE3-tuned HASH_CHUNK_SIZE is deliberately not reused.
HASH_WINDOW = 4 * 1024 * 1024


class UnsupportedCapture(RuntimeError):
    pass


# Command templates are kept in one table so each tool's syntax is visible in one place.
# Confirm the exact syntax of the build you install: these are not verified here.
TOOLS = {
    'WinPmem': dict(
        binary='winpmem.exe', os='Windows', arch=('amd64', 'x86_64'),
        args=lambda exe, dest: [exe, '-o', dest],
        privilege='Administrator',
        ready='Ready (verify build and privileges before acquisition)',
    ),
    'AVML': dict(
        binary='avml', os='Linux', arch=('x86_64', 'amd64'),
        args=lambda exe, dest: [exe, dest],          # LiME output, uncompressed
        privilege='root',
        ready='Ready (verify build and privileges before acquisition)',
    ),
    # Historical OSXPMem is intentionally not executable here: its CLI and kernel
    # compatibility have not been verified for modern macOS.
    'OSXPmem': dict(
        binary='osxpmem', os='Darwin', arch=(),
        args=None, privilege='root', ready='Unsupported: no verified macOS backend',
    ),
}


def native_architecture():
    """Return native OS architecture, not the architecture of an emulated Python process."""
    if os.name == 'nt':
        try:
            import ctypes
            native = ctypes.c_ushort()
            process = ctypes.c_ushort()
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            fn = kernel.IsWow64Process2
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(ctypes.c_ushort)]
            fn.restype = ctypes.c_int
            if fn(kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)):
                return {0xAA64: 'arm64', 0x8664: 'x86_64', 0x014c: 'x86'}.get(native.value, f'unknown-{native.value:x}')
        except (AttributeError, OSError, ValueError):
            pass
        # Windows also exposes the native architecture to emulated processes.
        arch = os.environ.get('PROCESSOR_ARCHITEW6432') or os.environ.get('PROCESSOR_ARCHITECTURE', '')
        return {'amd64': 'x86_64', 'arm64': 'arm64', 'x86': 'x86'}.get(arch.lower(), arch.lower() or 'unknown')
    return platform.machine().lower()


def _system_arch():
    return platform.system(), native_architecture()


def _unsupported_reason(name, system, arch):
    if name == 'OSXPmem' and system == 'Darwin' and arch == 'arm64':
        return 'Apple Silicon (arm64): no supported physical RAM acquisition tool exists'
    if name == 'OSXPmem' and system == 'Darwin':
        return 'RAM acquisition is currently unsupported on this macOS configuration.'
    if system == 'Windows' and arch == 'arm64' and name == 'WinPmem':
        return 'Windows ARM64: WinPmem x64 kernel driver is not compatible with the ARM64 kernel; a separately verified ARM64 driver/backend is required'
    return f'Unsupported on {system}/{arch}'


def is_elevated():
    if os.name == 'nt':
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def _candidate_executables(binary):
    """Find locally supplied acquisition binaries, including PyInstaller bundles."""
    roots = [Path(sys.executable).resolve().parent]
    if getattr(sys, 'frozen', False):
        roots.append(Path(getattr(sys, '_MEIPASS', roots[0])))
    else:
        roots.append(Path(__file__).resolve().parents[1])
    roots.extend([Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'HashTools',
                  Path('C:/ForensicTools/WinPmem')])
    override = os.environ.get('HASHTOOLS_WINPMEM_PATH', '').strip()
    candidates = [Path(override)] if override else []
    for root in roots:
        for subdir in ('', 'tools', 'tools/winpmem', 'bin'):
            candidates.append(root / subdir / binary)
    located = shutil.which(binary)
    if located:
        candidates.append(Path(located))
    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
            if resolved not in seen and resolved.is_file():
                seen.add(resolved)
                yield str(resolved)
        except OSError:
            continue


def _pe_machine(path):
    """Read PE COFF Machine without executing the file."""
    try:
        with open(path, 'rb') as stream:
            if stream.read(2) != b'MZ':
                return None
            stream.seek(0x3c)
            offset = int.from_bytes(stream.read(4), 'little')
            if offset < 64 or offset > 16 * 1024 * 1024:
                return None
            stream.seek(offset)
            if stream.read(4) != b'PE\0\0':
                return None
            return int.from_bytes(stream.read(2), 'little')
    except OSError:
        return None


def detect_backends():
    """Discover installed/bundled tools; do not execute or trust arbitrary downloads."""
    system, arch = _system_arch()
    results = []
    for name, spec in TOOLS.items():
        compatible = system == spec['os'] and arch in spec['arch'] and spec['args'] is not None
        candidates = list(_candidate_executables(spec['binary'])) if system == spec['os'] else []
        path = candidates[0] if candidates else None
        machine = _pe_machine(path) if path and system == 'Windows' else None
        machine_ok = (machine == 0x8664) if name == 'WinPmem' and path else True
        if not compatible:
            reason = _unsupported_reason(name, system, arch)
        elif not path:
            reason = ('WinPmem not supplied. Bundle a verified compatible build in '
                      'tools/winpmem or select a trusted local executable.') if name == 'WinPmem' else 'Acquisition tool not found on PATH'
        elif not machine_ok:
            reason = 'Executable is not a verified x64 PE binary (wrong architecture or invalid file)'
        else:
            reason = 'Executable located; driver compatibility and signature must be checked before acquisition'
        results.append({'name': name, 'path': path, 'compatible': compatible,
                        'ready': bool(path and compatible and machine_ok),
                        'reason': reason, 'privilege': spec['privilege'],
                        'machine': machine})
    return results


def request_administrator_restart():
    """Ask Windows UAC to relaunch the same application. Never bypass UAC."""
    if os.name != 'nt':
        raise UnsupportedCapture('Administrator restart is supported on Windows only')
    if is_elevated():
        return False
    import ctypes
    import subprocess
    if getattr(sys, 'frozen', False):
        executable = sys.executable
        parameters = subprocess.list2cmdline(sys.argv[1:])
    else:
        executable = sys.executable
        parameters = subprocess.list2cmdline([str(Path(sys.argv[0]).resolve()), *sys.argv[1:]])
    result = ctypes.windll.shell32.ShellExecuteW(None, 'runas', executable, parameters, None, 1)
    if result <= 32:
        raise OSError(f'UAC elevation was cancelled or failed (ShellExecuteW code {result})')
    return True


def _format_gib(n):
    return f'{n / GiB:.2f} GiB'


def _terminate(proc):
    """Stop the acquisition process and its children, escalating if needed."""
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                           capture_output=True, timeout=15)
        except subprocess.TimeoutExpired:
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name != 'nt':
            try:
                os.killpg(proc.pid, _SIGKILL)
            except ProcessLookupError:
                pass
        proc.kill()
        proc.wait()


def _supervise(proc, dest, expected, cancelled, progress):
    """Wait for the tool, reporting bytes written every second and honouring cancellation."""
    last = 0.0
    while proc.poll() is None:
        if cancelled():
            progress('Cancellation requested; stopping acquisition process')
            _terminate(proc)
            raise InterruptedError('Acquisition cancelled; partial file retained for inspection')
        now = time.monotonic()
        if now - last >= 1.0:
            last = now
            try:
                written = dest.stat().st_size
            except OSError:
                written = 0
            msg = f'Writing image: {_format_gib(written)}'
            if expected:
                msg += f' (installed RAM {_format_gib(expected)}; completion unknown)'
            progress(msg)
        time.sleep(0.1)


def hash_image(path, cancelled=lambda: False, progress=lambda msg: None):
    """Read the image once, feeding MD5 and SHA-256 from the same window.

    On multi-core hosts the two digests run on separate threads (hashlib releases the
    GIL for large buffers), so the total time approaches the slower digest instead of
    their sum. On single-CPU hosts it stays sequential to avoid thread overhead.
    Digests are identical either way.
    """
    md5 = hashlib.md5()
    sha = hashlib.sha256()
    total = os.path.getsize(path)
    buf = bytearray(HASH_WINDOW)
    concurrent = (os.cpu_count() or 1) >= 2
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='hash-sha') if concurrent else None
    done = 0
    last = time.monotonic()
    try:
        with open(path, 'rb', buffering=0) as f:
            while True:
                if cancelled():
                    raise InterruptedError('Hashing cancelled; the image was not validated')
                n = f.readinto(buf)
                if not n:
                    break
                piece = bytes(memoryview(buf)[:n])
                if pool is not None:
                    job = pool.submit(sha.update, piece)
                    md5.update(piece)
                    job.result()
                else:
                    md5.update(piece)
                    sha.update(piece)
                done += n
                now = time.monotonic()
                if now - last >= 1.0:
                    last = now
                    progress(f'Hashing: {_format_gib(done)} of {_format_gib(total)}')
    finally:
        if pool is not None:
            pool.shutdown(wait=True)
        del buf
    return {'md5': md5.hexdigest(), 'sha256': sha.hexdigest()}


def capture_memory(tool, executable, output, cancelled=lambda: False, progress=lambda msg: None, case_id=None, examiner=None):
    spec = TOOLS.get(tool)
    backend = next((b for b in detect_backends() if b['name'] == tool), None)
    if spec is None or backend is None or not backend['compatible'] or not backend['ready']:
        raise UnsupportedCapture(f'{tool} is not supported on this OS/architecture')
    if backend['path'] is None:
        raise UnsupportedCapture(f'{tool} was not detected on PATH')
    exe = Path(executable).expanduser().resolve()
    if not exe.is_file():
        raise FileNotFoundError(f'Acquisition executable not found: {exe}')
    if exe != Path(backend['path']).resolve():
        raise UnsupportedCapture('Select the detected executable; custom binaries are not automatically trusted')
    if not is_elevated():
        raise PermissionError(f'{tool} needs {spec["privilege"]} privileges. Restart HashTools with '
                              f'{spec["privilege"]} rights and try again. No acquisition was started.')

    dest = Path(output).expanduser().absolute()
    if not dest.parent.is_dir():
        raise FileNotFoundError(str(dest.parent))
    if dest.exists():
        raise FileExistsError(str(dest))
    if dest.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError('Use a .mem, .raw or .lime filename for this raw acquisition backend')
    log_path = dest.with_name(dest.name + '.acquisition.log')
    if log_path.exists():
        raise FileExistsError(str(log_path))
    for sidecar in (dest.with_suffix('.sha256'), dest.with_name('acquisition_metadata.json')):
        if sidecar.exists():
            raise FileExistsError(f'Sidecar already exists: {sidecar}')

    expected = psutil.virtual_memory().total
    free = shutil.disk_usage(dest.parent).free
    if expected and free < expected:
        raise OSError(f'Insufficient free space: {_format_gib(free)} available; installed RAM is {_format_gib(expected)}')
    args = spec['args'](str(exe), str(dest))
    progress('Starting ' + tool + ': ' + ' '.join(args))
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    logfile = open(log_path, 'x', encoding='utf-8')
    try:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=logfile, stderr=subprocess.STDOUT,
                                shell=False, start_new_session=(os.name != 'nt'))
    except OSError:
        logfile.close()
        log_path.unlink(missing_ok=True)   # nothing ran; do not leave an empty log behind
        raise
    with logfile:
        try:
            _supervise(proc, dest, expected, cancelled, progress)
        finally:
            if proc.poll() is None:
                _terminate(proc)
    acquisition_seconds = time.monotonic() - started

    if proc.returncode != 0:
        raise RuntimeError(f'{tool} exited with code {proc.returncode}. Inspect {log_path}; '
                           'any partial output was retained.')
    if not dest.is_file() or dest.stat().st_size == 0:
        raise RuntimeError('Acquisition returned success but no nonempty memory image was produced')

    size = dest.stat().st_size
    warnings = []
    if expected and size < MIN_EXPECTED_FRACTION * expected:
        warnings.append(f'Image is {size / expected:.0%} of installed RAM; the acquisition may be incomplete. '
                        f'Review {log_path.name} before relying on it.')
    progress(f'Acquisition complete ({_format_gib(size)} in {acquisition_seconds:.0f} s). '
             'Hashing with MD5 and SHA-256…')
    hash_started = time.monotonic()
    hashes = hash_image(dest, cancelled, progress)
    result = {
        'path': str(dest), 'size': size, 'tool': tool, 'log': str(log_path),
        'warnings': warnings,
        'acquisition_seconds': round(acquisition_seconds, 1),
        'hash_seconds': round(time.monotonic() - hash_started, 1),
        **hashes,
    }

    # Sidecars are written after successful acquisition and hashing. Never rewrite the image.
    ended_at = datetime.now(timezone.utc).isoformat()
    metadata = {
        'schema_version': 1, 'case_id': case_id or None, 'examiner': examiner or None,
        'acquisition_type': 'Physical RAM', 'hostname': platform.node(),
        'operating_system': platform.system(), 'os_version': platform.version(),
        'architecture': platform.machine(), 'cpu_model': platform.processor() or None,
        'total_ram_bytes': expected, 'acquisition_tool': tool,
        'acquisition_tool_version': None, 'output_filename': dest.name,
        'output_size_bytes': size, 'acquisition_start': started_at,
        'acquisition_end': ended_at, 'acquisition_duration_seconds': round(acquisition_seconds, 2),
        'sha256': hashes['sha256'], 'md5': hashes['md5'],
        'acquisition_status': 'completed_tool_reported_success',
        'warnings': warnings + ['Acquisition changes volatile memory; a SHA-256 digest does not prove completeness.'],
        'errors': [], 'command_argv': args, 'acquisition_log': log_path.name,
    }
    sha_path = dest.with_suffix('.sha256')
    metadata_path = dest.with_name('acquisition_metadata.json')
    for sidecar in (sha_path, metadata_path):
        if sidecar.exists():
            raise FileExistsError(f'Sidecar already exists; evidence preserved: {sidecar}')
    with sha_path.open('x', encoding='utf-8') as f:
        f.write(f"{hashes['sha256']}  {dest.name}\n")
    with metadata_path.open('x', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
        f.write('\n')
    result.update(metadata_path=str(metadata_path), sha256_file=str(sha_path))
    return result
