"""GUI-thread amount drafts; completed batches receive only immutable values."""
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QThread, Signal
from PySide6.QtGui import QColor

from ..input_config import parse_amount
from ..limits import MAX_SETS
from ..spec import AmountBatchSpec


class AmountInputModel(QAbstractTableModel):
    draft_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._count = 1
        self._drafts = [""]

    @property
    def set_count(self):
        return self._count

    def _gui_thread(self):
        if QThread.currentThread() != self.thread():
            raise RuntimeError("金额草稿只能在所属GUI线程读取或修改")

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self._count

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 2

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return ("来源套", "每套金额（元，正整数）")[section] if 0 <= section < 2 else None
        return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < self._count:
            return None
        row, column = index.row(), index.column()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return f"第{row + 1}套：000～999" if column == 0 else self._drafts[row]
        if column == 1 and role in (Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.ForegroundRole):
            try:
                parse_amount(self._drafts[row])
            except ValueError:
                return "请填写1～100位ASCII正整数，不允许0、小数、空白或科学计数法" if role == Qt.ItemDataRole.ToolTipRole else QColor("#b42318")
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return flags | Qt.ItemFlag.ItemIsEditable if index.column() == 1 else flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        self._gui_thread()
        if (role != Qt.ItemDataRole.EditRole or not index.isValid() or index.column() != 1
                or not 0 <= index.row() < self._count or type(value) is not str):
            return False
        if self._drafts[index.row()] == value:
            return True
        # Invalid and blank text are valid draft states, never valid batch input.
        self._drafts[index.row()] = value
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole,
                                           Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.ToolTipRole])
        self.draft_changed.emit()
        return True

    def set_set_count(self, count):
        self._gui_thread()
        if type(count) is not int or not 1 <= count <= MAX_SETS:
            raise ValueError(f"套数必须为1～{MAX_SETS}的整数")
        old = self._count
        if count == old:
            return
        if count > old:
            self.beginInsertRows(QModelIndex(), old, count - 1)
            self._drafts.extend([""] * max(0, count - len(self._drafts)))
            self._count = count
            self.endInsertRows()
        else:
            self.beginRemoveRows(QModelIndex(), count, old - 1)
            self._count = count
            self.endRemoveRows()
        # Hidden rows stay in _drafts, so reducing and then restoring S is undoable.
        self.draft_changed.emit()

    def amount_strings(self):
        self._gui_thread()
        return tuple(self._drafts[:self._count])

    @staticmethod
    def summarize(values):
        valid, invalid, subtotal = 0, [], 0
        for row, value in enumerate(values, 1):
            try:
                subtotal += parse_amount(value)
                valid += 1
            except ValueError:
                invalid.append(row)
        return {"set_count": len(values), "valid_count": valid, "invalid_sets": invalid,
                "subtotal": subtotal, "complete": not invalid, "raw_total": len(values) * 1000}

    def freeze_spec(self):
        self._gui_thread()
        return AmountBatchSpec(tuple(parse_amount(value) for value in self.amount_strings()))

    def draft_state(self):
        self._gui_thread()
        return {"set_count": self._count, "amounts": list(self._drafts)}

    def restore_draft(self, state):
        self._gui_thread()
        if type(state) is not dict or set(state) != {"set_count", "amounts"}:
            raise ValueError("草稿必须包含套数和金额文本")
        count, values = state["set_count"], state["amounts"]
        if (type(count) is not int or not 1 <= count <= MAX_SETS or type(values) is not list
                or not count <= len(values) <= MAX_SETS
                or any(type(value) is not str or len(value) > 32767 for value in values)):
            raise ValueError("草稿套数、行数或文本不合法")
        self.beginResetModel()
        self._count, self._drafts = count, list(values)
        self.endResetModel()
        self.draft_changed.emit()
