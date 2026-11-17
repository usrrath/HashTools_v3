from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = BASE_DIR / "assets"
# Not created at import time (a read-only install must still start).
# Create it with REPORTS_DIR.mkdir(exist_ok=True) only when a report is saved there.
REPORTS_DIR = BASE_DIR / "reports"

APP_NAME = "PAC HashTools"
ORG_NAME = "POLICE ACADEMY OF CAMBODIA"
KH_ORG_NAME = "បណ្ឌិត្យសភានគរបាលកម្ពុជា"
LOGO_PATH = ASSETS_DIR / "logo.png"

SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"}

HEX_BYTES_PER_ROW = 16
HEX_PAGE_ROWS = 256
HEX_PAGE_SIZE = HEX_BYTES_PER_ROW * HEX_PAGE_ROWS

# Memory-safe hashing: files are processed in bounded windows, never loaded whole.
# HASH_CHUNK_SIZE is the window size (also the cancel/pause/progress granularity).
# 16 MiB (measured best for BLAKE3 over mmap; 8-32 MiB are within ~5%): per-window
# thread hand-off cost is negligible and Pause/Cancel still react in ~50 ms.
HASH_CHUNK_SIZE = 16 * 1024 * 1024
# Memory-map large local files when BLAKE3 is selected (zero-copy, like `b3sum`):
# ~1.4x faster than read() for BLAKE3. Other algorithms are not faster mapped, so
# they always use read(). Set False for unreliable media (network shares, removable
# drives that may be unplugged mid-read): a mapped page that disappears raises
# SIGBUS, which read() would have reported as a normal I/O error.
HASH_USE_MMAP = True
# Files up to this size are read in a single call and hashed on the calling thread.
HASH_SMALL_FILE_LIMIT = 1024 * 1024

# Single source of truth for Evidence Explorer search paging (UI labels and docs follow it).
SEARCH_PAGE_SIZE = 150

REPORT_DEFAULT_NAME = str(REPORTS_DIR / "PAC_HashTools_Report.pdf")
