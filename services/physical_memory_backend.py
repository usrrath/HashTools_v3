"""External-tool physical RAM acquisition (FTK-Imager-style "Capture Memory") for
Windows x64 (WinPmem mini) and Linux x86_64 / aarch64 (AVML).

Only a user-supplied, trusted executable is used. No drivers or acquisition binaries are
bundled. An acquisition is reported as successful only when the tool exits with status 0,
a nonempty image exists, and the image has been hashed. Partial images, logs and a status
record are kept on failure or cancellation and are never marked as validated.

What this adds over a plain "run the tool, then hash" flow
---------------------------------------------------------
* The image is hashed *while it is being written* (``TailHasher``), so the long
  hash-after-capture wait disappears.
* Optional verification re-reads the finished image and confirms the streamed SHA-256, so
  the reported digests describe the bytes actually at rest.
* Determinate progress (percent, MiB/s, ETA) instead of an indeterminate bar.
* Per-image sidecars (``<image>.sha256/.md5/.sha1``, ``.metadata.json``, ``.summary.txt``)
  so several captures can live in one folder; the executable's own SHA-256 is recorded.
* Pre-flight checks that FTK-style imaging needs: FAT32 4 GiB file limit, free space,
  and a warning when the image is written to the system volume being acquired.
* A failed or cancelled capture still leaves a ``failed``/``cancelled`` status record.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

from config import settings
from services.memory_hashing import (
    DEFAULT_ALGORITHMS, HASH_ALGORITHMS, RateMeter, TailHasher, format_bytes, format_eta,
    hash_file, normalise_algorithms,
)

GiB = 1024 ** 3
MiB = 1024 ** 2
IMAGE_SUFFIXES = ('.mem', '.raw', '.lime')
# An image far smaller than installed RAM usually means an incomplete acquisition.
MIN_EXPECTED_FRACTION = 0.5
# Images are a little larger than RAM on some tools (headers / LiME segment records).
FREE_SPACE_MARGIN = 1.02
FAT_LIMIT = 4 * GiB - 1
_FAT_TYPES = {'fat', 'fat32', 'vfat', 'msdos'}
_SIGKILL = getattr(signal, 'SIGKILL', signal.SIGTERM)


class UnsupportedCapture(RuntimeError):
    pass


# The only WinPmem build HashTools runs: the mini x64 executable, invoked as
# ``winpmem_mini_x64.exe <output>`` (for example ``winpmem_mini_x64.exe physmem.raw``).
# Other WinPmem builds and syntaxes (rc2, go-winpmem, legacy ``-o``) are deliberately not run.
WINPMEM_BINARY = 'winpmem_mini_x64.exe'


# Command templates are kept in one table so each tool's syntax is visible in one place.
TOOLS = {
    'WinPmem': dict(
        binary=WINPMEM_BINARY, binaries=(WINPMEM_BINARY,), patterns=(),
        os='Windows', arch=('amd64', 'x86_64'),
        args=lambda exe, dest: [exe, dest],          # winpmem_mini_x64.exe <output>
        privilege='Administrator',
        ready='Ready (verify build and privileges before acquisition)',
    ),
    'AVML': dict(
        binary='avml', binaries=('avml',), patterns=(),
        os='Linux', arch=('x86_64', 'amd64', 'aarch64', 'arm64'),
        args=lambda exe, dest: [exe, dest],          # LiME output, uncompressed
        privilege='root',
        ready='Ready (verify build and privileges before acquisition)',
        ready_by_arch={'aarch64': 'Ready on aarch64: confirm your AVML build supports this architecture',
                       'arm64': 'Ready on aarch64: confirm your AVML build supports this architecture'},
    ),
    # Historical OSXPMem is intentionally not executable here: its CLI and kernel
    # compatibility have not been verified for modern macOS.
    'OSXPmem': dict(
        binary='osxpmem', binaries=('osxpmem',), patterns=(), os='Darwin', arch=(),
        args=None, privilege='root', ready='Unsupported: no verified macOS backend',
    ),
}

# Tools the examiner explicitly approved in this session ({name: absolute path}).
_CUSTOM_TOOLS: dict = {}


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
    return {'amd64': 'x86_64', 'aarch64': 'arm64'}.get(platform.machine().lower(), platform.machine().lower())


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


def local_tool_dirs():
    """Folders searched, besides PATH, for a trusted tool shipped next to the application
    (``<app>/tools``). The working directory is deliberately NOT searched."""
    dirs = [Path(settings.BASE_DIR) / 'tools']
    if getattr(sys, 'frozen', False):
        dirs.insert(0, Path(sys.executable).resolve().parent / 'tools')
    return dirs


def _find_tool(spec):
    """Return ``(path, source)`` for the first executable found, else ``(None, None)``."""
    for name in spec.get('binaries') or (spec['binary'],):
        found = shutil.which(name)
        if found:
            return found, 'PATH'
    for folder in local_tool_dirs():
        try:
            if not folder.is_dir():
                continue
            for name in spec.get('binaries', ()):
                if (folder / name).is_file():
                    return str(folder / name), 'tools folder'
            for pattern in spec.get('patterns', ()):
                for match in sorted(folder.glob(pattern)):
                    if match.is_file():
                        return str(match), 'tools folder'
        except OSError:
            continue
    return None, None


def register_custom_tool(name, path):
    """Approve a specific executable the examiner browsed to. Returns its SHA-256 so the UI
    can show it for confirmation. The binary is still only run for a compatible OS/arch."""
    spec = TOOLS.get(name)
    if spec is None or spec['args'] is None:
        raise UnsupportedCapture(f'{name} has no supported acquisition command')
    system, arch = _system_arch()
    if system != spec['os'] or arch not in spec['arch']:
        raise UnsupportedCapture(_unsupported_reason(name, system, arch))
    exe = Path(path).expanduser().resolve()
    if not exe.is_file():
        raise FileNotFoundError(f'Not a file: {exe}')
    if os.name != 'nt' and not os.access(exe, os.X_OK):
        raise PermissionError(f'Tool is not executable: {exe}')
    # The command syntax is fixed per binary name, so a renamed or different build is refused.
    if exe.name.lower() not in {b.lower() for b in spec['binaries']}:
        raise UnsupportedCapture(f'For {name} select the file named {" or ".join(spec["binaries"])}; '
                                 f'got {exe.name}')
    digest = hash_file(exe, ('sha256',))['sha256']
    _CUSTOM_TOOLS[name] = str(exe)
    return digest


def clear_custom_tools():
    _CUSTOM_TOOLS.clear()


def detect_backends():
    """Return one status record per known tool. Cheap: safe to call on every user action."""
    system, arch = _system_arch()
    results = []
    for name, spec in TOOLS.items():
        compatible = system == spec['os'] and arch in spec['arch'] and spec['args'] is not None
        # Report the detected binary even when incompatible, but never execute it.
        path, source = _find_tool(spec) if system == spec['os'] else (None, None)
        if compatible and name in _CUSTOM_TOOLS and Path(_CUSTOM_TOOLS[name]).is_file():
            path, source = _CUSTOM_TOOLS[name], 'user-approved'
        if compatible and path and os.name != 'nt' and not os.access(path, os.X_OK):
            reason = f'Acquisition tool is not executable: {path}. Check file permissions.'
            results.append({'name': name, 'path': path, 'source': source, 'compatible': True,
                            'ready': False, 'reason': reason, 'privilege': spec['privilege']})
            continue
        if compatible and path:
            reason = spec.get('ready_by_arch', {}).get(arch, spec['ready'])
            if source and source != 'PATH':
                reason += f' [{source}: {Path(path).name}]'
        elif compatible:
            reason = (f'Not installed: place {" or ".join(spec["binaries"])} on PATH or in the tools folder, '
                      'or use Browse tool, then press Re-check tools')
        else:
            reason = _unsupported_reason(name, system, arch)
        results.append({
            'name': name, 'path': path, 'source': source, 'compatible': compatible,
            'ready': bool(path and compatible), 'reason': reason,
            'privilege': spec['privilege'],
        })
    return results


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


def _supervise(proc, dest, expected, cancelled, progress, report=None, tail=None):
    """Wait for the tool, reporting size/rate/ETA twice a second and honouring cancellation."""
    last = 0.0
    meter = RateMeter()
    while True:
        try:
            proc.wait(timeout=0.1)      # returns the moment the tool exits
            return
        except subprocess.TimeoutExpired:
            pass
        if cancelled():
            progress('Cancellation requested; stopping acquisition process')
            _terminate(proc)
            raise InterruptedError('Acquisition cancelled; partial file retained for inspection')
        now = time.monotonic()
        if now - last < 0.5:
            continue
        last = now
        try:
            written = dest.stat().st_size
        except OSError:
            written = 0
        meter.update(written)
        # Installed RAM is not a reliable total for a sparse or segmented image.
        # The acquisition tool has not provided a trustworthy completion percentage.
        msg = f'Writing image: {_format_gib(written)} (completion unknown)'
        msg += f' · {meter.rate / MiB:.0f} MiB/s · ETA unavailable'
        if tail is not None:
            msg += f' · hashed {_format_gib(tail.done)}'
        progress(msg)
        if report is not None:
            report({'phase': 'acquire', 'done': written, 'total': None, 'percent': None,
                    'rate': meter.rate, 'eta': None, 'hashed': tail.done if tail else 0})


def hash_image(path, cancelled=lambda: False, progress=lambda msg: None,
               algorithms=DEFAULT_ALGORITHMS, report=None):
    """Read the image once (read-only), feeding all selected digests in parallel.

    Returns ``{'md5': ..., 'sha256': ...}`` (plus ``sha1`` when requested). ``progress``
    receives text lines; ``report`` receives structured dicts (percent, MiB/s, ETA).
    """
    total = os.path.getsize(path)
    last = [0.0]

    def relay(info):
        if report is not None:
            report(info)
        now = time.monotonic()
        if now - last[0] >= 1.0:
            last[0] = now
            progress(f'Hashing: {_format_gib(info["done"])} of {_format_gib(total)} '
                     f'({info["percent"]:.0f}%) · {info["rate"] / MiB:.0f} MiB/s · '
                     f'ETA {format_eta(info["eta"])}')

    return hash_file(path, algorithms, cancelled, relay)


def sidecar_paths(dest):
    """Per-image sidecar names, so several captures can share one folder."""
    name = dest.name
    return {
        'log': dest.with_name(name + '.acquisition.log'),
        'metadata': dest.with_name(name + '.metadata.json'),
        'summary': dest.with_name(name + '.summary.txt'),
        **{a: dest.with_name(f'{name}.{a}') for a in HASH_ALGORITHMS},
    }


def _mount_for(path):
    best, best_len = None, -1
    p = str(Path(path).resolve())
    for part in psutil.disk_partitions(all=True):
        mp = part.mountpoint
        if (p == mp or p.startswith(mp.rstrip('\\/') + os.sep) or mp == '/') and len(mp) > best_len:
            best, best_len = part, len(mp)
    return best


def destination_checks(folder, expected):
    """Raise for destinations that cannot hold the image; return non-blocking warnings."""
    warnings = []
    try:
        part = _mount_for(folder)
    except OSError:
        part = None
    if part is not None and part.fstype.lower() in _FAT_TYPES and expected > FAT_LIMIT:
        raise OSError(f'The destination volume is {part.fstype} (max file size 4 GiB) but installed '
                      f'RAM is {_format_gib(expected)}. Choose an NTFS/exFAT/ext4 volume.')
    free = shutil.disk_usage(folder).free
    if expected and free < expected * FREE_SPACE_MARGIN:
        raise OSError(f'Insufficient free space: {_format_gib(free)} available; '
                      f'installed RAM is {_format_gib(expected)}')
    try:
        system_root = (os.environ.get('SystemDrive', 'C:') + '\\') if os.name == 'nt' else '/'
        if os.stat(folder).st_dev == os.stat(system_root).st_dev:
            warnings.append('Image is being written to the system volume that is being acquired. This overwrites '
                            'unallocated space and can destroy evidence. Prefer an external evidence drive.')
    except OSError:
        pass
    return warnings


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _write_new(path, text):
    with path.open('x', encoding='utf-8') as f:
        f.write(text)


def _write_summary(path, meta, hashes, verified, source):
    lines = [
        'PAC HashTools - Memory Acquisition Summary', '=' * 44,
        f"Case ID:            {meta.get('case_id') or ''}",
        f"Examiner:           {meta.get('examiner') or ''}",
        f"Host:               {meta['hostname']} ({meta['operating_system']} {meta['architecture']})",
        f"Installed RAM:      {format_bytes(meta['total_ram_bytes'])}",
        f"Tool:               {meta['acquisition_tool']} {meta.get('acquisition_tool_version') or ''}".rstrip(),
        f"Tool SHA-256:       {meta.get('tool_sha256')}",
        f"Command:            {' '.join(meta['command_argv'])}",
        f"Acquisition start:  {meta['acquisition_start']}",
        f"Acquisition end:    {meta['acquisition_end']}",
        '', f"Image:              {meta['output_filename']}",
        f"Image size:         {meta['output_size_bytes']} bytes ({format_bytes(meta['output_size_bytes'])})",
    ]
    for algo in HASH_ALGORITHMS:
        if algo in hashes:
            lines.append(f"{algo.upper() + ' checksum:':<20}{hashes[algo]}")
    lines += ['', f'Hash source:        {source}',
              'Verification:       ' + ('re-read of the finished image confirmed the streamed SHA-256'
                                         if verified else 'NOT independently re-read'),
              '', 'Notes:']
    lines += [f'  - {w}' for w in meta['warnings']]
    _write_new(path, '\n'.join(lines) + '\n')


def _record_outcome(paths, base, status, error):
    """Persist a failed/cancelled status record next to the retained partial image."""
    meta = {**base, 'acquisition_status': status, 'acquisition_end': _utc(), 'errors': [str(error)],
            'validated': False}
    try:
        _write_new(paths['metadata'], json.dumps(meta, indent=2, ensure_ascii=False) + '\n')
    except OSError:
        pass   # never mask the original failure


def _tool_failure_details(log_path, exit_code):
    """Explain external acquisition failures without guessing driver-specific causes."""
    signed = exit_code
    if os.name == 'nt' and isinstance(exit_code, int):
        signed = ((exit_code + 2**31) % 2**32) - 2**31
    try:
        # Read only the last 64 KiB: tool logs can be very large.
        with open(log_path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 65536))
            tail = handle.read().decode('utf-8', errors='replace')
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
        excerpt = '\n'.join(lines[-12:])
    except OSError as exc:
        excerpt = f'Unable to read acquisition log: {exc}'
    guidance = (
        'Check the acquisition log for a driver-load or security-policy error. '
        'Confirm that the WinPmem build and its signed driver support this Windows '
        'kernel and security configuration. Do not disable Windows security protections. '
        'If no compatible driver is available, use Live-Response ZIP collection.'
    )
    return (f'Acquisition tool exited with code {exit_code} (signed {signed}). '
            f'Log: {log_path}\nLast tool output:\n{excerpt or "(no diagnostic output)"}\n{guidance}')


def capture_memory(tool, executable, output, cancelled=lambda: False, progress=lambda msg: None,
                   case_id=None, examiner=None, *, algorithms=('md5', 'sha1', 'sha256'),
                   verify=True, report=None):
    """Acquire physical RAM with an approved external tool and hash the image.

    ``verify=True`` (recommended) re-reads the finished image and confirms the SHA-256 that
    was computed while the tool was writing. ``verify=False`` skips that re-read and trusts
    the streamed digests (fastest; relies on the tool writing sequentially).
    ``report`` receives structured progress dicts; ``progress`` receives text lines.
    """
    algos = normalise_algorithms(algorithms)
    if 'sha256' not in algos:
        algos = (*algos, 'sha256')  # mandatory forensic integrity digest
    spec = TOOLS.get(tool)
    backend = next((b for b in detect_backends() if b['name'] == tool), None)
    if spec is None or backend is None or not backend['compatible'] or not backend['ready']:
        raise UnsupportedCapture(f'{tool} is not supported on this OS/architecture')
    if backend['path'] is None:
        raise UnsupportedCapture(f'{tool} was not detected on PATH')
    exe = Path(executable).expanduser().resolve()
    if not exe.is_file():
        raise FileNotFoundError(f'Acquisition executable not found: {exe}')
    if os.name != 'nt' and not os.access(exe, os.X_OK):
        raise PermissionError(f'Acquisition tool is not executable: {exe}')
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
    paths = sidecar_paths(dest)
    for key, p in paths.items():
        if p.exists():
            raise FileExistsError(f'{"Log" if key == "log" else "Sidecar"} already exists: {p}')

    expected = psutil.virtual_memory().total
    warnings = destination_checks(dest.parent, expected)
    tool_sha256 = hash_file(exe, ('sha256',))['sha256']
    tool_version = None   # not probed: running an unverified binary with extra args has side effects; the SHA-256 identifies the build
    args = spec['args'](str(exe), str(dest))
    progress('Starting ' + tool + ': ' + subprocess.list2cmdline(args))
    progress(f'Tool SHA-256: {tool_sha256}')
    for w in warnings:
        progress('WARNING: ' + w)
    started_at = _utc()
    started = time.monotonic()
    base = {
        'schema_version': 2, 'case_id': case_id or None, 'examiner': examiner or None,
        'acquisition_type': 'Physical RAM', 'hostname': platform.node(),
        'operating_system': platform.system(), 'os_version': platform.version(),
        'architecture': native_architecture(), 'process_architecture': platform.machine(), 'cpu_model': platform.processor() or None,
        'total_ram_bytes': expected, 'acquisition_tool': tool, 'acquisition_tool_version': tool_version,
        'tool_path': str(exe), 'tool_sha256': tool_sha256, 'tool_source': backend.get('source'),
        'elevated': True, 'output_filename': dest.name, 'acquisition_start': started_at,
        'command_argv': args, 'acquisition_log': paths['log'].name, 'hash_algorithms': list(algos),
    }

    logfile = open(paths['log'], 'x', encoding='utf-8')
    try:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=logfile, stderr=subprocess.STDOUT,
                                shell=False, start_new_session=(os.name != 'nt'))
    except OSError:
        logfile.close()
        paths['log'].unlink(missing_ok=True)   # nothing ran; do not leave an empty log behind
        raise
    tail = TailHasher(dest, algos)
    tail.start()
    try:
        with logfile:
            try:
                _supervise(proc, dest, expected, cancelled, progress, report, tail)
            finally:
                if proc.poll() is None:
                    _terminate(proc)
        acquisition_seconds = time.monotonic() - started
        if proc.returncode != 0:
            raise RuntimeError(_tool_failure_details(paths['log'], proc.returncode) + '\nAny partial output was retained.')
        if not dest.is_file() or dest.stat().st_size == 0:
            raise RuntimeError('Acquisition returned success but no nonempty memory image was produced')
        size = dest.stat().st_size
        progress(f'Acquisition complete ({_format_gib(size)} in {acquisition_seconds:.0f} s).')
        progress(f'Hashing: finalizing streamed digests for {_format_gib(size)}')
        hash_started = time.monotonic()
        tail.request_finish()
        meter = RateMeter()
        while tail.is_alive():                 # normally already (almost) caught up
            tail.join(0.2)
            if cancelled():
                raise InterruptedError('Hashing cancelled; the image was not validated')
            if report is not None:
                meter.update(tail.done)
                report({'phase': 'hash', 'done': tail.done, 'total': size,
                        'percent': min(100.0, tail.done * 100.0 / size), 'rate': meter.rate,
                        'eta': meter.eta(tail.done, size)})
        streamed = tail.digests
    except InterruptedError as exc:
        tail.abort()
        _record_outcome(paths, base, 'cancelled', exc)
        raise
    except BaseException as exc:
        tail.abort()
        _record_outcome(paths, base, 'failed', exc)
        raise

    warnings = list(warnings)
    if expected and size < MIN_EXPECTED_FRACTION * expected:
        warnings.append(f'Image is {size / expected:.0%} of installed RAM; the acquisition may be incomplete. '
                        f'Review {paths["log"].name} before relying on it.')
    verified, source, verify_seconds = False, 'streamed', 0.0
    try:
        usable = streamed is not None and tail.valid and tail.done == size == dest.stat().st_size
        if usable and verify:
            vt = time.monotonic()
            algo = 'sha256' if 'sha256' in algos else algos[0]
            progress(f'Verifying: re-reading the finished image ({algo.upper()})…')
            check = hash_file(dest, (algo,), cancelled, report, phase='verify')
            verify_seconds = time.monotonic() - vt
            if check[algo] == streamed[algo]:
                hashes, verified, source = streamed, True, 'streamed, confirmed by re-read'
            else:
                warnings.append(f'{algo.upper()} of the finished file differs from the hash computed while '
                                'writing (the tool may write non-sequentially, or the file changed). '
                                'Full re-read digests are reported.')
                hashes, source = hash_file(dest, algos, cancelled, report, phase='verify'), 're-read'
        elif usable:
            hashes = streamed
            warnings.append('Verification re-read was skipped: digests were computed while the tool was '
                            'writing and assume sequential output.')
        else:
            warnings.append('Streamed hashing was not usable (file size changed or tool wrote out of order); '
                            'digests come from a full re-read.')
            hashes, source = hash_file(dest, algos, cancelled, report, phase='verify'), 're-read'
    except InterruptedError as exc:
        _record_outcome(paths, base, 'cancelled_during_verification', exc)
        raise
    hash_seconds = time.monotonic() - hash_started
    ended_at = _utc()
    ordered = {a: hashes[a] for a in algos}
    result = {
        'path': str(dest), 'size': size, 'tool': tool, 'log': str(paths['log']),
        'tool_sha256': tool_sha256, 'tool_version': tool_version,
        'warnings': warnings, 'verified': verified, 'hash_source': source,
        'acquisition_seconds': round(acquisition_seconds, 1),
        'hash_seconds': round(hash_seconds, 1), 'verify_seconds': round(verify_seconds, 1),
        **ordered,
    }

    metadata = {**base, 'output_size_bytes': size, 'acquisition_end': ended_at,
                'acquisition_duration_seconds': round(acquisition_seconds, 2),
                'hashes': ordered, **ordered, 'hash_source': source, 'verified_by_reread': verified,
                'acquisition_status': 'completed_tool_reported_success', 'validated': verified,
                'warnings': warnings + ['Acquisition changes volatile memory; a digest does not prove completeness.'],
                'errors': []}
    # Sidecars are written after successful acquisition and hashing. Never rewrite the image.
    for algo, digest in ordered.items():
        _write_new(paths[algo], f'{digest}  {dest.name}\n')       # `sha256sum -c` compatible
    _write_new(paths['metadata'], json.dumps(metadata, indent=2, ensure_ascii=False) + '\n')
    _write_summary(paths['summary'], metadata, ordered, verified, source)
    result.update(metadata_path=str(paths['metadata']), summary_path=str(paths['summary']),
                  sha256_file=str(paths['sha256']) if 'sha256' in ordered else None)
    return result


# --------------------------------------------------------------------------- verification
def _read_reference_hashes(image):
    """Hashes recorded by a previous acquisition, if its sidecars sit next to the image."""
    image = Path(image)
    refs = {}
    for algo in HASH_ALGORITHMS:
        for candidate in (image.with_name(image.name + '.' + algo), image.with_suffix('.' + algo)):
            try:
                token = candidate.read_text(encoding='utf-8').split()[0].strip().lower()
            except (OSError, IndexError, UnicodeDecodeError):
                continue
            if len(token) == {'md5': 32, 'sha1': 40, 'sha256': 64}[algo]:
                refs[algo] = (token, candidate.name)
                break
    size = None
    try:
        meta = json.loads(image.with_name(image.name + '.metadata.json').read_text(encoding='utf-8'))
        size = meta.get('output_size_bytes')
        for algo in HASH_ALGORITHMS:
            if algo not in refs and isinstance(meta.get(algo), str):
                refs[algo] = (meta[algo].lower(), image.name + '.metadata.json')
    except (OSError, ValueError):
        pass
    return refs, size


def verify_image(path, expected=None, cancelled=lambda: False, progress=lambda msg: None,
                 algorithms=('md5', 'sha1', 'sha256'), report=None):
    """Hash an existing image read-only and compare it with a typed hash and/or the sidecars
    written at acquisition time. ``verification`` is MATCH, MISMATCH or NO REFERENCE."""
    algos = normalise_algorithms(algorithms)
    refs, ref_size = _read_reference_hashes(path)
    typed = (expected or '').strip().lower()
    if typed:
        algo = {32: 'md5', 40: 'sha1', 64: 'sha256'}.get(len(typed))
        if algo is None or any(c not in '0123456789abcdef' for c in typed):
            raise ValueError('Expected hash must be 32 (MD5), 40 (SHA-1) or 64 (SHA-256) hex characters')
        refs[algo] = (typed, 'typed value')
        algos = normalise_algorithms((*algos, algo))
    size = os.path.getsize(path)
    hashes = hash_image(path, cancelled, progress, algos, report)
    comparisons = [{'algorithm': a, 'expected': refs[a][0], 'actual': hashes[a], 'source': refs[a][1],
                    'match': refs[a][0] == hashes[a]} for a in algos if a in refs]
    size_ok = None if ref_size is None else (ref_size == size)
    if comparisons and all(c['match'] for c in comparisons) and size_ok is not False:
        outcome = 'MATCH'
    elif comparisons or size_ok is False:
        outcome = 'MISMATCH'
    else:
        outcome = 'NO REFERENCE'
    return {'path': str(path), 'size': size, **hashes, 'verification': outcome,
            'comparisons': comparisons, 'size_matches_record': size_ok}
