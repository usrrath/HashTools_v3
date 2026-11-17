from PyQt6.QtWidgets import QPushButton

def make_button(text: str, primary: bool = False) -> QPushButton:
    btn = QPushButton(text)
    if primary:
        btn.setStyleSheet("QPushButton{background:#2563eb;color:white;border:0;border-radius:8px;padding:9px 16px;font-weight:700;} QPushButton:hover{background:#1d4ed8;}")
    else:
        btn.setStyleSheet("QPushButton{background:#ffffff;color:#111827;border:1px solid #cbd5e1;border-radius:8px;padding:8px 14px;} QPushButton:hover{background:#f8fafc;}")
    return btn
