"""Export structured forensic analysis without overwriting evidence."""
import csv
import json
from pathlib import Path

def export_json(path, data):
    with Path(path).open('x',encoding='utf-8') as f: json.dump(data,f,indent=2,default=str)
    return str(path)

def export_csv(path, rows):
    rows=list(rows)
    if not rows: raise ValueError('No rows to export')
    columns=list(dict.fromkeys(k for row in rows for k in row))
    with Path(path).open('x',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=columns); writer.writeheader(); writer.writerows(rows)
    return str(path)
