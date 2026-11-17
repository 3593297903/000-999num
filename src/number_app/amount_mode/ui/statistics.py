"""All 1000 codes in one model, using independently read actual saved data."""
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import QLabel, QTableView, QVBoxLayout, QWidget


class StatisticsModel(QAbstractTableModel):
    HEADERS = ("号码", "预期原始份数", "实际原始份数", "份数差值", "预期累计金额", "实际累计金额", "金额差值")
    KEYS = ("code", "expected_raw", "actual_raw", "raw_difference", "expected_amount", "actual_amount", "amount_difference")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.KEYS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role == Qt.ItemDataRole.DisplayRole:
            return str(self.rows[index.row()][self.KEYS[index.column()]])
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section] if orientation == Qt.Orientation.Horizontal else str(section + 1)
        return None


class StatisticsWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.table_model = StatisticsModel(self)
        self.table_view = QTableView()
        self.table_view.setModel(self.table_model)
        self.table_view.setAlternatingRowColors(True)
        self.table_view.horizontalHeader().setStretchLastSection(True)
        self.summary_label = QLabel("逐号统计覆盖000～999；份数与金额分别核对")
        self.summary_label.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.table_view)

    def set_store(self, store):
        rows = store.get_statistics()
        self.table_model.set_rows(rows)
        differences = sum(row["raw_difference"] != 0 or row["amount_difference"] != "0" for row in rows)
        self.summary_label.setText(f"批次{store.batch_id}：实际读取全部1000个号码，差异{differences}个。金额单位：元。")

    def clear_store(self):
        self.table_model.set_rows([])
        self.summary_label.setText('请选择已完成且最近完整核查通过的批次')

    def open_batch(self, catalog, batch_id, store=None):
        if store is None:
            from .group_index import ResultStore
            store = ResultStore(catalog, batch_id)
        self.set_store(store)
