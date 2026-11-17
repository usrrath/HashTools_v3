from pathlib import Path
from datetime import datetime
import ast
import shutil
import sys

root = Path(__file__).resolve().parent
project = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else Path.cwd().resolve()
if not (project / 'app/tabs/hash_tab.py').is_file() or not (project / 'services/hash_service.py').is_file():
    raise SystemExit('Expected HashTools project root as current directory or first argument.')

# Upgrade both modules as one operation; backups make rollback straightforward.
files = ['app/tabs/hash_tab.py', 'services/hash_service.py']
for relative in files:
    ast.parse((root / relative).read_text(encoding='utf-8'), filename=relative)
stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
for relative in files:
    dst = project / relative
    backup = dst.with_name(dst.name + '.backup_' + stamp)
    shutil.copy2(dst, backup)
    shutil.copy2(root / relative, dst)
    print('Updated:', dst)
    print('Backup: ', backup)

req = project / 'requirements.txt'
text = req.read_text(encoding='utf-8')
if not any(line.strip().lower().startswith('blake3') for line in text.splitlines()):
    with req.open('a', encoding='utf-8') as handle:
        handle.write('\n# BLAKE3 digest (b3sum compatible)\nblake3>=1.0,<2\n')
    print('Updated:', req)
print('Install dependency: ./.venv/bin/python -m pip install -r requirements.txt')
print('Run: ./.venv/bin/python main.py')
