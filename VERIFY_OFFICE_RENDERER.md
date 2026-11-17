# Check the rendered Office viewer

The viewer converts a *temporary copy* with LibreOffice and rasterizes the PDF with PyMuPDF or QtPdf.
Installing PyMuPDF does not install LibreOffice.

All commands run from the project folder (the one containing `main.py`).

1. Install LibreOffice from https://www.libreoffice.org/download/download-libreoffice/ and open it once.
2. Find the executable and check it:
   - macOS: `/Applications/LibreOffice.app/Contents/MacOS/soffice --version`
   - Windows: `"C:\Program Files\LibreOffice\program\soffice.exe" --version`
   - Linux: `soffice --version`
   If it is not on PATH, set `HASHTOOL_SOFFICE` to the full path.
3. Create ONE virtual environment in this folder and install into it:
   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt          # Windows: .venv\Scripts\python
   .venv/bin/python -c "import pymupdf; print(pymupdf.VersionBind)"
   ```
4. Start the app with the same interpreter: `.venv/bin/python main.py`, select an Office file and read the
   status text under the preview if conversion fails.

Rendered pages include images and charts as far as LibreOffice preserves them; fonts missing on the
machine change the layout. Qt font warnings are separate from conversion failures.
