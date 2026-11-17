"""HashTool v2 entry point. Configure Qt logging before importing PyQt6."""
import os

# FFmpeg initialization is informational; the Qt font database warning is
# generally harmless. Hide these two categories without disabling errors.
# Set HASHTOOL_QT_DEBUG=1 to retain all Qt diagnostic messages.
if os.environ.get("HASHTOOL_QT_DEBUG") != "1":
    current = os.environ.get("QT_LOGGING_RULES", "").strip()
    rules = "qt.multimedia.ffmpeg.info=false;qt.text.font.db.warning=false;qt.qpa.fonts.warning=false"
    os.environ["QT_LOGGING_RULES"] = (current + ";" + rules) if current else rules

from app.main_window import run_app

if __name__ == "__main__":
    run_app()
