from __future__ import annotations

import base64
import html
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

from PyQt6.QtCore import QSizeF, QMarginsF
from PyQt6.QtGui import QFont, QFontDatabase, QTextDocument, QPageSize, QPageLayout
from PyQt6.QtPrintSupport import QPrinter

from config.settings import LOGO_PATH, ORG_NAME, KH_ORG_NAME
from utils.file_utils import human_size
from services.metadata_service import MetadataService


class PDFReportService:
    """Clean A4 landscape PDF-only report exporter for PAC HashTools.

    Fixed in this version:
    - PDF uses the exact file names stored in the GUI list; no inserted <wbr/> marks.
    - Report title/default primary file ignores OS cache entries such as .DS_Store.
    - Logo is constrained by width/height attributes so Qt cannot render it oversized.
    - Header is compact and friendly, with Khmer Unicode font fallback.
    - Hash columns are wrapped by controlled line breaks to avoid overlap.
    """

    KHMER_FONT_CANDIDATES = [
        "Khmer Sangam MN",
        "Khmer MN",
        "Noto Sans Khmer",
        "Khmer OS Siemreap",
        "Khmer OS Battambang",
        "Khmer OS",
        "DaunPenh",
        "Leelawadee UI",
        "Arial Unicode MS",
        "DejaVu Sans",
        "Arial",
        "Sans Serif",
    ]

    MONO_FONT_STACK = '"Menlo", "Consolas", "Courier New", "DejaVu Sans Mono", monospace'
    IGNORED_PRIMARY_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}

    def generate(
        self,
        output_path: str,
        current_file: str | None,
        metadata_rows: list[tuple[str, str]],
        hash_results: list,
        progress_callback=None,
        algorithms=None,
    ) -> None:
        if progress_callback:
            progress_callback(5, "Preparing PDF report...")

        out = Path(output_path)
        if out.suffix.lower() != ".pdf":
            out = out.with_suffix(".pdf")
        out.parent.mkdir(parents=True, exist_ok=True)

        if progress_callback:
            progress_callback(15, "Loading Khmer Unicode fonts...")
        self._load_khmer_fonts()
        font_family = self._best_khmer_font_family()

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(out))
        printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        printer.setPageOrientation(QPageLayout.Orientation.Landscape)
        printer.setPageMargins(QMarginsF(10, 9, 10, 9), QPageLayout.Unit.Millimeter)
        printer.setFullPage(False)
        printer.setCreator("PAC HashTools")
        printer.setDocName(self._doc_name(current_file, hash_results))

        if progress_callback:
            progress_callback(45, "Building friendly PDF layout...")
        document = QTextDocument()
        document.setDefaultFont(QFont(font_family, 9))
        page_rect = printer.pageRect(QPrinter.Unit.Point)
        document.setPageSize(QSizeF(page_rect.width(), page_rect.height()))
        document.setHtml(self._build_html(current_file, metadata_rows, hash_results, font_family, algorithms))

        if progress_callback:
            progress_callback(80, "Writing PDF file...")
        document.print(printer)
        if progress_callback:
            progress_callback(100, "PDF report exported successfully.")

    def _build_html(
        self,
        current_file: str | None,
        metadata_rows: list[tuple[str, str]],
        hash_results: list,
        font_family: str,
        algorithms=None,
    ) -> str:
        generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        font_stack = self._css_font_stack(font_family)
        title = self._doc_name(current_file, hash_results)
        per_file_metadata = self._collect_all_file_metadata(hash_results, metadata_rows)
        return f"""
<!doctype html>
<html lang="km">
<head>
<meta charset="utf-8">
<style>
  * {{ font-family: {font_stack}; }}
  body {{ margin: 0; padding: 0; color: #111827; font-size: 8.7pt; line-height: 1.34; }}
  table {{ width: 100%; border-collapse: collapse; }}
  .header {{
    width: 100%;
    border: none;
    border-bottom: 2px solid #1e40af;
    margin-bottom: 12px;
    text-align: center;
  }}
  .header td {{
    border: none;
    padding: 8px 0 10px 0;
    vertical-align: middle;
    text-align: center;
  }}
  .logo {{
    width: 70px;
    height: 70px;
    margin-bottom: 7px;
  }}
  .kh-title {{ font-size: 16pt; font-weight: 800; color: #1e3a8a; text-align: center; }}
  .en-title {{ font-size: 11pt; font-weight: 800; color: #111827; margin-top: 2px; text-align: center; }}
  .report-title {{ font-size: 13.5pt; font-weight: 900; color: #0f172a; margin-top: 6px; text-align: center; }}
  .sub-line {{ font-size: 7.9pt; color: #475569; margin-top: 3px; text-align: center; }}
  .badge {{ color: #1d4ed8; font-weight: 800; }}
  h2 {{ font-size: 10.6pt; color: #1e40af; margin: 10px 0 5px 0; padding-bottom: 3px; border-bottom: 1px solid #bfdbfe; }}
  th, td {{ border: 1px solid #cbd5e1; padding: 4px 5px; vertical-align: top; }}
  th {{ background: #e8f0fe; color: #0f172a; font-weight: 800; }}
  .summary td {{ width: 20%; }}
  .metric {{ background: #f8fafc; border: 1px solid #dbeafe; padding: 6px; text-align: center; }}
  .metric-label {{ color: #475569; font-size: 7.7pt; }}
  .metric-value {{ color: #1e40af; font-size: 12pt; font-weight: 900; }}
  .meta-key {{ width: 24%; background: #f8fafc; font-weight: 800; color: #334155; }}
  .hash-table {{ table-layout: fixed; }}
  .hash-table th {{ text-align: left; }}
  .col-no {{ width: 4%; text-align: center; }}
  .col-file {{ width: 30%; }}
  .col-size {{ width: 8%; }}
  .col-md5 {{ width: 15%; }}
  .col-sha1 {{ width: 18%; }}
  .col-sha256 {{ width: 25%; }}
  .file-name {{ font-weight: 800; color: #0f172a; }}
  .file-path {{ font-size: 7.1pt; color: #64748b; margin-top: 2px; }}
  .mono {{ font-family: {self.MONO_FONT_STACK}; font-size: 7.1pt; line-height: 1.18; color: #111827; }}
  .small {{ font-size: 7.8pt; color: #475569; }}
  .note {{ border: 1px solid #f59e0b; background: #fffbeb; padding: 7px; margin-top: 10px; font-size: 8pt; }}
  .footer {{ margin-top: 12px; padding-top: 6px; border-top: 1px solid #cbd5e1; color: #64748b; font-size: 7.8pt; text-align: center; }}
  a {{ color: #1d4ed8; text-decoration: underline; font-weight: 800; }}
</style>
</head>
<body>
  <table class="header">
    <tr>
      <td>
        <div>{self._logo_html()}</div>
        <div class="kh-title">{self._e(KH_ORG_NAME)}</div>
        <div class="en-title">{self._e(ORG_NAME)}</div>
        <div class="report-title">{self._e(title)}</div>
        <div class="sub-line">Generated: {self._e(generated)} &nbsp;|&nbsp; Export Format: PDF</div>
      </td>
    </tr>
  </table>

  {self._summary_html(current_file, metadata_rows, hash_results, per_file_metadata)}
  {self._hash_html(hash_results, algorithms)}
  {self._all_files_metadata_html(per_file_metadata)}

  <div class="note">របាយការណ៍នេះត្រូវបានបង្កើតជា PDF ប៉ុណ្ណោះ។ ឈ្មោះឯកសារ និងតម្លៃ hash ដែលបានជ្រើសរើស ត្រូវបានរៀបចំឱ្យអានងាយ និងមិនជាន់គ្នា។</div>
  <div class="footer">PAC HashTools - File Analysis Suite | Police Academy of Cambodia</div>
</body>
</html>
"""

    def _clean_results(self, hash_results: Iterable) -> list:
        results = list(hash_results or [])
        return [r for r in results if getattr(r, "file_name", "") not in self.IGNORED_PRIMARY_NAMES]

    def _doc_name(self, current_file: str | None, hash_results: Iterable) -> str:
        results = self._clean_results(hash_results)
        if len(results) > 1:
            return "PAC Hash Report"
        if len(results) == 1:
            return "PAC HashTools File Analysis Report"
        return "PAC HashTools Analysis Report"

    def _primary_file_name(self, current_file: str | None = None, hash_results: Iterable | None = None) -> str:
        """Primary File display was removed from the PDF header.

        Kept only for backward compatibility with older code that may call it.
        """
        return ""

    def _summary_html(
        self,
        current_file: str | None,
        metadata_rows: list[tuple[str, str]],
        hash_results: list,
        per_file_metadata: list[tuple[object, list[tuple[str, str]]]] | None = None,
    ) -> str:
        results = self._clean_results(hash_results)
        file_count = len(results)
        metadata_count = sum(len(rows) for _, rows in (per_file_metadata or []))
        if not metadata_count:
            metadata_count = len(metadata_rows or [])
        total_bytes = 0
        for r in results:
            try:
                total_bytes += int(getattr(r, "size", 0) or 0)
            except Exception:
                pass
        if current_file and not total_bytes:
            try:
                total_bytes = Path(current_file).stat().st_size
            except OSError:
                total_bytes = 0
        return f"""
<h2>Report Summary</h2>
<table class="summary">
<tr>
  <td><div class="metric"><div class="metric-label">Files</div><div class="metric-value">{file_count}</div></div></td>
  <td><div class="metric"><div class="metric-label">Total Size</div><div class="metric-value">{self._e(human_size(total_bytes))}</div></div></td>
  <td><div class="metric"><div class="metric-label">Hash Algorithms</div><div class="metric-value">3</div></div></td>
  <td><div class="metric"><div class="metric-label">Metadata Rows</div><div class="metric-value">{metadata_count}</div></div></td>
  <td><div class="metric"><div class="metric-label">Page</div><div class="metric-value">A4</div></div></td>
</tr>
</table>
"""

    def _collect_all_file_metadata(
        self,
        hash_results: Iterable,
        fallback_metadata_rows: list[tuple[str, str]] | None = None,
    ) -> list[tuple[object, list[tuple[str, str]]]]:
        """Collect metadata for every file included in the PDF report.

        The Metadata tab normally contains metadata only for the currently selected file.
        The PDF report needs metadata for all generated hash result rows, so this
        method re-extracts metadata from each result's file_path during export.
        """
        results = self._clean_results(hash_results)
        metadata_service = MetadataService()
        collected: list[tuple[object, list[tuple[str, str]]]] = []

        for result in results:
            file_path = getattr(result, "file_path", "") or ""
            rows: list[tuple[str, str]] = []
            if file_path:
                try:
                    if Path(file_path).exists():
                        rows = metadata_service.extract(file_path)
                    else:
                        rows = [
                            ("[File System] Name", str(getattr(result, "file_name", "N/A") or "N/A")),
                            ("[File System] Path", str(file_path)),
                            ("[File System] Status", "File path not available at export time"),
                        ]
                except Exception as exc:
                    rows = [
                        ("[File System] Name", str(getattr(result, "file_name", "N/A") or "N/A")),
                        ("[File System] Path", str(file_path)),
                        ("[Metadata] Error", str(exc)),
                    ]
            collected.append((result, rows))

        if not collected and fallback_metadata_rows:
            collected.append((None, list(fallback_metadata_rows)))
        return collected

    def _all_files_metadata_html(self, per_file_metadata: list[tuple[object, list[tuple[str, str]]]]) -> str:
        if not per_file_metadata:
            return "<h2>Metadata for All Files</h2><p class='small'>No metadata available.</p>"

        sections: list[str] = ["<h2>Metadata for All Files</h2>"]
        for idx, (result, rows) in enumerate(per_file_metadata, start=1):
            file_name = "Selected File"
            file_path = ""
            size_text = ""
            if result is not None:
                file_name = str(getattr(result, "file_name", "N/A") or "N/A")
                file_path = str(getattr(result, "file_path", "") or "")
                try:
                    size_text = human_size(int(getattr(result, "size", 0) or 0))
                except Exception:
                    size_text = "N/A"

            body = []
            for key, value in rows:
                key_text = str(key)
                value_text = str(value)
                if key_text == "[GPS] Google Maps" and value_text.startswith("https://"):
                    value_html = f'<a href="{self._e(value_text)}">Open Location in Google Maps</a><br/><span class="small">{self._e(value_text)}</span>'
                else:
                    value_html = self._e(value_text)
                body.append(
                    f"<tr><td class='meta-key'>{self._e(key_text)}</td>"
                    f"<td>{value_html}</td></tr>"
                )
            if not body:
                body.append("<tr><td colspan='2' class='small'>No metadata available for this file.</td></tr>")

            subtitle = f"File {idx}: {self._e(file_name)}"
            if size_text:
                subtitle += f" <span class='small'>({self._e(size_text)})</span>"
            path_line = f"<div class='file-path'>{self._e(file_path)}</div>" if file_path else ""
            sections.append(
                f"<h3 style='font-size:9.2pt; color:#0f172a; margin:8px 0 4px 0;'>{subtitle}</h3>"
                f"{path_line}"
                "<table><tr><th class='meta-key'>Property</th><th>Value</th></tr>"
                + "".join(body)
                + "</table>"
            )
        return "".join(sections)

    def _metadata_html(self, rows: list[tuple[str, str]]) -> str:
        # Backward-compatible helper for older callers. The PDF now uses
        # _all_files_metadata_html() so every hash result gets its own metadata.
        return self._all_files_metadata_html([(None, rows or [])])

    def _hash_html(self, results: Iterable, algorithms=None) -> str:
        results = self._clean_results(results)
        algorithms = tuple(algorithms or ("md5", "sha1", "sha256", "sha512"))
        if not results:
            return "<h2>Hash Results</h2><p>No results available.</p>"
        headers = ("No", "File Name / Path", "Size") + tuple(a.upper() for a in algorithms)
        rows = ["<tr>" + "".join(f"<th>{self._e(h)}</th>" for h in headers) + "</tr>"]
        for idx, r in enumerate(results, 1):
            cells = [str(idx), self._e(str(getattr(r, "file_name", ""))) + "<div class='file-path'>" + self._e(str(getattr(r, "file_path", ""))) + "</div>", self._e(human_size(getattr(r, "size", 0) or 0))]
            cells += [self._wrap_hash(getattr(r, name, "") or "N/A", 24) for name in algorithms]
            rows.append("<tr>" + "".join(f"<td>{v}</td>" for v in cells) + "</tr>")
        return "<h2>Hash Results - " + " / ".join(a.upper() for a in algorithms) + "</h2><table class='hash-table'>" + "".join(rows) + "</table>"

    def _warning_html(self, result) -> str:
        warning = getattr(result, "warning", "") or ""
        error = getattr(result, "error", "") or ""
        text = error if (error and not getattr(result, "success", True)) else warning
        return f"<br/><span style='color:#b45309'>&#9888; {self._e(text)}</span>" if text else ""

    def _logo_html(self) -> str:
        if not LOGO_PATH.exists():
            return ""
        try:
            data = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
            suffix = LOGO_PATH.suffix.lower().replace(".", "") or "png"
            mime = "jpeg" if suffix in {"jpg", "jpeg"} else suffix
            # Width/height attributes are important: QTextDocument does not fully support CSS max-width/object-fit.
            return f'<img class="logo" src="data:image/{mime};base64,{data}" width="70" height="70" />'
        except Exception:
            return ""

    def _load_khmer_fonts(self) -> None:
        font_paths = [
            "/System/Library/Fonts/Supplemental/Khmer Sangam MN.ttf",
            "/System/Library/Fonts/Khmer Sangam MN.ttf",
            "/Library/Fonts/Khmer Sangam MN.ttf",
            "/Library/Fonts/Khmer MN.ttf",
            "C:/Windows/Fonts/daunpenh.ttf",
            "C:/Windows/Fonts/KhmerUI.ttf",
            "C:/Windows/Fonts/NotoSansKhmer-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansKhmer-Regular.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansKhmer-Regular.ttf",
            "/usr/local/share/fonts/NotoSansKhmer-Regular.ttf",
        ]
        assets_font_dir = Path(__file__).resolve().parent.parent / "assets" / "fonts"
        if assets_font_dir.exists():
            font_paths.extend(str(p) for p in assets_font_dir.glob("*.ttf"))
            font_paths.extend(str(p) for p in assets_font_dir.glob("*.otf"))
        for fp in font_paths:
            try:
                p = Path(fp)
                if p.exists():
                    QFontDatabase.addApplicationFont(str(p))
            except Exception:
                pass

    def _best_khmer_font_family(self) -> str:
        available = set(QFontDatabase.families())
        for family in self.KHMER_FONT_CANDIDATES:
            if family in available:
                return family
        return "Arial"

    def _css_font_stack(self, primary: str) -> str:
        families = [primary] + [f for f in self.KHMER_FONT_CANDIDATES if f != primary]
        return ", ".join(f'"{f}"' for f in families)

    @staticmethod
    def _e(value: str) -> str:
        return html.escape(str(value), quote=True).replace("\n", "<br/>")

    def _wrap_hash(self, value: str, chunk: int = 32) -> str:
        value = re.sub(r"\s+", "", str(value))
        if not value or value == "N/A":
            return self._e(value or "N/A")
        pieces = [value[i: i + chunk] for i in range(0, len(value), chunk)]
        return "<br/>".join(self._e(p) for p in pieces)
