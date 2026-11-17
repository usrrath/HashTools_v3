# PAC HashTools (HashTool v2)

Desktop tool (PyQt6) for file hashing, metadata, hex viewing, evidence browsing and PDF reports.
Evidence is only ever opened read-only; previews use temporary copies.

## Install and run
```bash
python -m pip install -r requirements.txt     # use the SAME Python that runs the app
python main.py
```
Run the checks: `python -m compileall -q .` and `python -m unittest discover -s tests -v`.

## Features
- **Hashing Calculator** – MD5, SHA-1, SHA-256, SHA-512 and BLAKE3 (b3sum-compatible) in one pass (16 MiB windows, one thread per algorithm, never loads a whole file).
  A cancelled or failed file never produces a hash. A file that changes while it is read is marked ⚠.
- **MetaData** – file-system times shown in UTC and local time. "Created (birth time)" appears only where the OS
  provides it; on Linux/macOS the inode-change time is labelled "Metadata changed (ctime)". Image EXIF and GPS.
- **Hex View** – paged hex/ASCII view, hex or text search, signature highlight.
- **Evidence Explorer** – file browser with preview and "Search Everything": indexes the folder once, then instant
  search-as-you-type (`report`, `*.pdf`, `ext:pdf;docx`, `parent:cases`, `size:>10mb`, `dm:>2024-01-01`, `!draft`, `a | b`,
  `regex:"^IMG_\d+"`; hover the search box for the full syntax). Use **Re-index** if files changed.
- **Report** – A4 landscape PDF with MD5, SHA-1, SHA-256 and SHA-512 per file, plus metadata.

### Evidence Explorer search
Search scans the selected Browse Root recursively and stores *all* matches in a temporary SQLite
database (not capped). Results are shown in **150-row pages** (`SEARCH_PAGE_SIZE` in `config/settings.py`).
Terms combine with AND. Examples: `*.exe`, `ext:pdf`, `parent:/Users/name/Downloads *.docx`,
`path:reports`, `file: *.xlsx`, `folder: photos`. It is a filesystem scan, not a persistent index.
Temporary index files are deleted when the window closes.

### Office previews
- `.docx`, `.pptx`, `.xlsx`: bounded text extraction built in.
- Rendered pages (and legacy `.doc`, `.ppt`, `.xls`): need a local LibreOffice. The file is converted from a private
  temporary copy with a temporary profile; the whole LibreOffice process tree is killed on timeout.
  Set `HASHTOOL_SOFFICE` to the executable if it is not found automatically (see `VERIFY_OFFICE_RENDERER.md`).
- Rendering uses PyMuPDF or QtPdf; text extraction is the fallback. Layout can differ from Microsoft Office.
- Conversion currently runs on the UI thread and can pause the window on large documents.

### Qt messages on macOS
`qt.multimedia.ffmpeg` and `qt.text.font.db` lines are informational. Set `HASHTOOL_QT_DEBUG=1` to show them.

## Known limits
- Folder scanning runs on the UI thread (once per selection, about 18 µs per file on a warm cache).
- The Hashing table keeps one row per file in memory (about 3 MB per 1,000 rows); very large batches
  (millions of files) need a virtual table model.
- Not tested in this repository: real LibreOffice conversion, packaged macOS/Windows builds.

## PDF and Khmer preview update (2026-11-16)
- PDF preview falls back to PyMuPDF when QtPdf is unavailable; page navigation and zoom remain available.
- Full hash PDF export now uses Qt's Unicode text shaping for Khmer names/paths; install Noto Sans Khmer or Khmer Sangam MN before exporting.
- Office conversion uses LibreOffice and depends on fonts installed on the host. Missing Khmer glyphs in a converted PDF cannot be repaired merely by changing the preview renderer. Install the source document's Khmer fonts and reconvert.
- Full PDF hash export uses vector text and a compressed Qt PDF writer; long paths may be visually elided in the PDF, while complete paths remain in the SQLite database.
