# Hex View universal preview update

- Hex View right panel now has Text, Office Viewer, Image, Hex, PDF and Video/Audio tabs.
- Uses private temporary copies; the original evidence is never written to.
- Office files require LibreOffice installed and discoverable; slides/documents convert to PDF using the existing isolated conversion service.
- PDF and Office page rendering require PyMuPDF; multimedia playback requires Qt Multimedia and platform codecs.
- Copy/conversion happens in a QThread; the preview is capped at 300 MiB per file and text/hex display is bounded.
- Page controls show one PDF/Office page at a time.
- This is not a verified macOS GUI test; test with your own DOCX/PPTX/PDF/MP4 examples.
