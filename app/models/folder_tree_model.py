"""Lazy filesystem tree with lightweight folder selection.

Qt's QFileSystemModel enumerates expanded directories asynchronously. Checkbox
selection is explicit and does not recurse through descendants or modify files.
"""
from pathlib import Path
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFileSystemModel


class EvidenceFolderModel(QFileSystemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._checked: set[str] = set()
        self.setReadOnly(True)
        self.setResolveSymlinks(False)

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and index.column() == 0 and self.isDir(index):
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and index.column() == 0 and role == Qt.ItemDataRole.CheckStateRole and self.isDir(index):
            return Qt.CheckState.Checked if self.filePath(index) in self._checked else Qt.CheckState.Unchecked
        return super().data(index, role)

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role == Qt.ItemDataRole.CheckStateRole and index.isValid() and index.column() == 0 and self.isDir(index):
            path = self.filePath(index)
            if int(value) == int(Qt.CheckState.Checked.value):
                self._checked.add(path)
            else:
                self._checked.discard(path)
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
            return True
        return super().setData(index, value, role)

    def selected_folders(self):
        return sorted(self._checked)

    def clear_selected(self):
        if not self._checked:
            return
        self._checked.clear()
        # Avoid resolving potentially millions of paths on the GUI thread.
        # Only repaint currently visible indexes through the view's viewport.
        self.layoutChanged.emit()
