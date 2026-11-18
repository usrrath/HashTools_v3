"""Evidence acquisition report (HTML; can print to PDF)."""
import html
from pathlib import Path

def write_html(path, record):
    rows=''.join('<tr><th>'+html.escape(str(k))+'</th><td>'+html.escape(str(v))+'</td></tr>' for k,v in record.items())
    document='<!doctype html><html><head><meta charset="utf-8"><title>Memory Evidence Report</title></head><body><h1>Memory Evidence Report</h1><table>'+rows+'</table></body></html>'
    Path(path).write_text(document,encoding='utf-8') if not Path(path).exists() else (_ for _ in ()).throw(FileExistsError(path))
    return str(path)
