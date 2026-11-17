#!/usr/bin/env python3
"""Patch the HashTools installation containing this script, without replacing other modules."""
from pathlib import Path
import ast
import shutil
import sys
from datetime import datetime

root = Path(__file__).resolve().parent
hex_file = root / 'app/tabs/hex_tab.py'
viewer_file = root / 'app/tabs/universal_preview.py'
if not hex_file.is_file() or not viewer_file.is_file():
    sys.exit('ERROR: Place this script in the hashTool_v2 project root (next to main.py).')
source = hex_file.read_text(encoding='utf-8')
if 'class HexViewTab(' not in source:
    sys.exit('ERROR: HexViewTab not found. No changes made.')
if 'class UniversalPreview(' not in viewer_file.read_text(encoding='utf-8'):
    sys.exit('ERROR: UniversalPreview not found. No changes made.')

if 'self.universal_preview = UniversalPreview(' in source and 'self.universal_preview.load_file(' in source and 'Preview / File Details' not in source:
    print('ALREADY UPDATED:', hex_file)
    sys.exit(0)

old = source
import_anchor = 'from utils.ui_utils import make_button'
if 'from app.tabs.universal_preview import UniversalPreview' not in source:
    if import_anchor not in source:
        sys.exit('ERROR: import anchor missing; no changes made.')
    source = source.replace(import_anchor, import_anchor + '\nfrom app.tabs.universal_preview import UniversalPreview', 1)

start = source.find('        self.preview = QLabel("No preview")')
end = source.find('        self.info = QLabel(', start)
if start == -1 or end == -1:
    sys.exit('ERROR: old image-only preview block missing; no changes made.')
source = source[:start] + '''        heading = QLabel("Temporary copy preview")
        heading.setObjectName("hexPreviewHeading")
        rv.addWidget(heading)
        self.universal_preview = UniversalPreview(right)
        self.universal_preview.setObjectName("hexUniversalSixTabPreview")
        self.universal_preview.setMinimumHeight(270)
        rv.addWidget(self.universal_preview, 8)
''' + source[end:]

old_clear = '        self.preview.clear()\n        self.preview.setText("No preview")'
if old_clear not in source:
    sys.exit('ERROR: old preview reset not found; no changes made.')
source = source.replace(old_clear, '        self.universal_preview.reset()', 1)

start = source.find('    def load_preview(self) -> None:')
end = source.find('    def render(self) -> None:', start)
if start == -1 or end == -1:
    sys.exit('ERROR: old preview loader not found; no changes made.')
source = source[:start] + '''    def load_preview(self) -> None:
        if self.current_file:
            self.universal_preview.load_file(self.current_file)
        else:
            self.universal_preview.reset()

''' + source[end:]

old_split = '        splitter.setSizes([980, 300])'
if old_split in source:
    source = source.replace(old_split, '''        right.setMinimumWidth(430)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 5)
        splitter.setSizes([800, 800])''', 1)

ast.parse(source, filename=str(hex_file))
if 'Preview / File Details' in source or 'self.preview.' in source:
    sys.exit('ERROR: old preview references remain; no changes made.')
backup = hex_file.with_name('hex_tab.py.backup_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
shutil.copy2(hex_file, backup)
tmp = hex_file.with_suffix('.py.tmp')
try:
    tmp.write_text(source, encoding='utf-8')
    tmp.replace(hex_file)
except Exception:
    tmp.unlink(missing_ok=True)
    raise
print('SUCCESS: six-tab viewer connected to HexViewTab')
print('Updated:', hex_file)
print('Backup:', backup)
print('Start application with: ./.venv/bin/python main.py')
