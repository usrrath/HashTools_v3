"""Stream complete SQLite hash results to a compact Unicode PDF using Qt text shaping.

Qt uses platform font fallback for Khmer shaping. For reliable output install a
Khmer-capable font (Noto Sans Khmer or Khmer Sangam MN) before exporting.
"""
from __future__ import annotations
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QFont, QFontDatabase, QPainter, QPdfWriter, QColor


def _font():
    from PyQt6.QtCore import QCoreApplication
    if QCoreApplication.instance() is None:
        raise RuntimeError('PDF export requires an initialized Qt application')
    candidates = ('Noto Sans Khmer', 'Khmer Sangam MN', 'Khmer OS', 'Khmer UI', 'DaunPenh')
    families = set(QFontDatabase.families())
    for family in candidates:
        if family in families:
            return QFont(family, 8)
    # Qt may substitute a fallback font; report this rather than silently losing Khmer.
    raise RuntimeError('Khmer font unavailable. Install Noto Sans Khmer or Khmer Sangam MN before exporting.')


def export_all_hashes(db_path, output_path, progress=None, cancelled=lambda: False, algorithms=None):
    """Constant-memory export with Khmer-shaped filenames/paths and vector text."""
    if not db_path or not os.path.isfile(db_path):
        raise FileNotFoundError('Hash results database is missing.')
    algorithms = tuple(algorithms or ("md5", "sha1", "sha256", "sha512"))
    font = _font()
    out = Path(output_path)
    tmp = out.with_name(out.name + '.partial.pdf')
    conn = sqlite3.connect(f'file:{Path(db_path).resolve().as_posix()}?mode=ro', uri=True, timeout=30)
    painter = QPainter()
    try:
        total = conn.execute('SELECT COUNT(*) FROM results').fetchone()[0]
        if not total:
            raise ValueError('No stored hash results to export.')
        writer = QPdfWriter(str(tmp))
        writer.setResolution(96)
        writer.setTitle('HashTools | Complete Hash Evidence Report')
        writer.setCreator('HashTools')
        writer.setPageSize(__import__('PyQt6.QtGui', fromlist=['QPageSize']).QPageSize(__import__('PyQt6.QtGui', fromlist=['QPageSize']).QPageSize.PageSizeId.A4))
        writer.setPageOrientation(__import__('PyQt6.QtGui', fromlist=['QPageLayout']).QPageLayout.Orientation.Landscape)
        if not painter.begin(writer):
            raise RuntimeError('Could not initialize PDF writer')
        w, h = writer.width(), writer.height()
        margin = 38
        line_h = 15
        row_h = max(68, 58 + len(algorithms) * line_h)
        page = 0
        y = 0
        def draw(text, x, yy, width=None, bold=False, size=8):
            f = QFont(font)
            f.setPointSize(size)
            f.setBold(bold)
            painter.setFont(f)
            painter.setPen(QColor('#172033'))
            painter.drawText(QRectF(x, yy, width or (w-x-margin), line_h),
                             Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextSingleLine,
                             str(text))
        def header():
            nonlocal page, y
            if page:
                writer.newPage()
            page += 1
            painter.fillRect(0, 0, w, 67, QColor('#eaf0f8'))
            draw('HashTools | Complete Hash Evidence Report', margin, 12, bold=True, size=13)
            draw(f'{datetime.now():%Y-%m-%d %H:%M:%S}   |   Total records: {total:,}   |   Page {page}', margin, 39)
            y = 82
        header()
        cursor = conn.execute('SELECT id,path,size,md5,sha1,sha256,sha512,success,error,warning FROM results ORDER BY id')
        for done, (rid, path, size, md5, sha1, sha256, sha512, success, error, warning) in enumerate(cursor, 1):
            if cancelled():
                raise InterruptedError('PDF export cancelled')
            if y + row_h > h - 24:
                header()
            if done % 2:
                painter.fillRect(margin-6, y, w-2*margin+12, row_h-3, QColor('#f3f6fa'))
            # Elide exceptionally long paths for readable layout. Full paths remain in SQLite.
            from PyQt6.QtGui import QFontMetrics
            fm = QFontMetrics(font)
            path_text = fm.elidedText(str(path or ''), Qt.TextElideMode.ElideMiddle, w-2*margin-80)
            draw(f'{rid:,}. {Path(path or "").name}', margin, y, bold=True)
            draw(f'Path: {path_text}', margin, y+line_h)
            draw(f'Size: {size or 0:,} bytes    Status: {"OK" if success else "ERROR"}', margin, y+2*line_h)
            for j, (label, value) in enumerate((('MD5',md5),('SHA1',sha1),('SHA256',sha256),('SHA512',sha512)), 3):
                draw(f'{label}: {value or ""}', margin, y+j*line_h)
            if not success:
                draw('Error: '+fm.elidedText(str(error or ''), Qt.TextElideMode.ElideRight, w-2*margin-80), margin, y+(3+len(algorithms))*line_h)
            y += row_h
            if progress and done % 250 == 0:
                progress(done, total)
        painter.end()
        os.replace(tmp, out)
        if progress:
            progress(total, total)
        return total
    except BaseException:
        if painter.isActive():
            painter.end()
        tmp.unlink(missing_ok=True)
        raise
    finally:
        conn.close()
