"""Verify the active HashTools source tree and six-tab Hex View at runtime."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
print("Python executable:", sys.executable)
print("Project directory:", ROOT)
from app.tabs import hex_tab, universal_preview
print("Loaded Hex View module:", Path(hex_tab.__file__).resolve())
print("Loaded preview module:", Path(universal_preview.__file__).resolve())
from PyQt6.QtWidgets import QApplication, QLabel
app = QApplication.instance() or QApplication(sys.argv)
w = hex_tab.HexViewTab()
labels = [w.universal_preview.tabs.tabText(i) for i in range(w.universal_preview.tabs.count())]
print("Preview tabs:", labels)
print("Old image-only placeholder present:", bool(w.findChildren(QLabel, "legacyImagePreview")))
assert labels == ["Text", "Office Viewer", "Image", "Hex", "PDF", "Video/Audio"]
print("PASS: Six-tab Hex View loaded from this project.")
w.close()
