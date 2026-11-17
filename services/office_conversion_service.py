"""Isolated LibreOffice conversion for evidence preview; never writes to source."""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import signal
import subprocess


def locate_soffice() -> str:
    override = os.environ.get('HASHTOOL_SOFFICE', '').strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        located = shutil.which(override)
        if located:
            return located
        raise RuntimeError(f'HASHTOOL_SOFFICE points to a missing/non-executable program: {override}')
    options = [shutil.which('soffice'), shutil.which('libreoffice')]
    if os.name == 'posix':
        options.extend([
            '/Applications/LibreOffice.app/Contents/MacOS/soffice',
            str(Path.home() / 'Applications/LibreOffice.app/Contents/MacOS/soffice'),
            '/opt/homebrew/bin/soffice', '/usr/local/bin/soffice',
        ])
    for option in options:
        if option and Path(option).is_file() and os.access(option, os.X_OK):
            return str(option)
    raise RuntimeError('LibreOffice not found. Install LibreOffice or set HASHTOOL_SOFFICE to the executable path.')


def kill_process_tree(proc: "subprocess.Popen") -> None:
    """Kill the launcher AND its children (soffice starts soffice.bin as a child)."""
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                           capture_output=True, check=False, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def convert_office(source: Path, workspace: Path, fmt: str, extension: str, *, timeout: int = 120) -> Path:
    """Convert a private copy. Workspace must be application-owned temporary storage."""
    if fmt not in {'pdf', 'docx', 'pptx', 'xlsx'}:
        raise ValueError('Unsupported conversion format')
    if extension != '.' + fmt:
        raise ValueError('Unexpected output extension')
    executable = locate_soffice()
    source = Path(source)
    if not source.is_file() or source.is_symlink():
        raise ValueError('Office preview source must be a regular temporary file')
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    input_dir = workspace / 'input'
    output_dir = workspace / 'output'
    profile_dir = workspace / 'profile'
    for folder in (input_dir, output_dir, profile_dir):
        folder.mkdir(exist_ok=True, mode=0o700)
    copied = input_dir / ('office_input' + source.suffix.lower())
    if copied.resolve() == source.resolve():
        raise ValueError('Conversion input and original cannot be the same path')
    shutil.copyfile(source, copied)
    result_path = output_dir / ('office_input' + extension)
    command = [executable, '-env:UserInstallation=' + profile_dir.resolve().as_uri(),
               '--headless', '--nologo', '--nodefault', '--nofirststartwizard',
               '--convert-to', ('pdf:impress_pdf_Export' if fmt == 'pdf' and source.suffix.lower() in {'.ppt', '.pptx', '.pps', '.ppsx', '.odp'} else fmt), '--outdir', str(output_dir), str(copied)]
    popen_kwargs = {'start_new_session': True} if os.name != 'nt' else {
        'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)}
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=str(workspace), **popen_kwargs)
    except OSError as exc:
        raise RuntimeError(f'Unable to start LibreOffice: {exc}') from exc
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        kill_process_tree(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        raise RuntimeError(f'LibreOffice conversion timed out after {timeout} seconds') from exc
    except BaseException:
        kill_process_tree(proc)
        raise
    if proc.returncode != 0 or not result_path.is_file() or result_path.stat().st_size == 0:
        details = ((stderr or '') + '\n' + (stdout or '')).strip()[-700:]
        raise RuntimeError(f'LibreOffice conversion failed (exit {proc.returncode}): {details or "no output created"}')
    return result_path
