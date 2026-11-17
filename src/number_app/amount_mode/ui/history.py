from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtWidgets import QWidget, QTableView, QVBoxLayout, QHBoxLayout, QPushButton, QLabel


class HistoryModel(QAbstractTableModel):
    fields = [('batch_id', '批次'), ('created_at', '创建时间'), ('set_count', '套数'),
              ('per_code_expected_amount', '金额列合计'), ('group_count', '组数'),
              ('generation_status', '生成状态'), ('latest_verification_status', '上次核查'), ('backup_status', '本批备份')]
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
    def rowCount(self, parent=QModelIndex()): return 0 if parent.isValid() else len(self.rows)
    def columnCount(self, parent=QModelIndex()): return len(self.fields)
    def data(self, index, role=Qt.DisplayRole):
        if index.isValid() and role == Qt.DisplayRole:
            return str(self.rows[index.row()].get(self.fields[index.column()][0], '') or '')
    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole:
            return self.fields[section][1] if orientation == Qt.Horizontal else section + 1
    def replace_rows(self, rows):
        self.beginResetModel(); self.rows = rows; self.endResetModel()


class HistoryWidget(QWidget):
    batch_selected = Signal(str)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.catalog = None
        self.page = 0
        self.page_size = 30
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('历史显示上次核查结论；打开批次时会重新核对固定文件指纹。'))
        self.model = HistoryModel(self)
        self.table = QTableView(); self.table.setObjectName('amountHistoryTable')
        self.table.setModel(self.model); self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setEditTriggers(QTableView.NoEditTriggers)
        self.table.doubleClicked.connect(self.open_selected)
        layout.addWidget(self.table)
        bar = QHBoxLayout()
        for text, action in [('上一页', self.previous), ('下一页', self.next_page), ('刷新历史', self.refresh), ('打开选中批次', self.open_selected)]:
            button = QPushButton(text); button.clicked.connect(action); bar.addWidget(button)
        self.page_label = QLabel(); bar.addWidget(self.page_label)
        layout.addLayout(bar)
    def set_catalog(self, catalog): self.catalog = catalog; self.page = 0; self.refresh()
    def refresh(self):
        if self.catalog is None: return
        count = self.catalog.count()
        self.page = min(self.page, max(0, (count - 1) // self.page_size))
        self.model.replace_rows(self.catalog.list_page(offset=self.page * self.page_size, limit=self.page_size))
        self.page_label.setText(f'第{self.page + 1}页，共{count}批（每页{self.page_size}批）')
        self.table.resizeColumnsToContents()
    def previous(self): self.page = max(0, self.page - 1); self.refresh()
    def next_page(self): self.page += 1; self.refresh()
    def open_selected(self, index=None):
        selected = self.table.currentIndex()
        if selected.isValid(): self.batch_selected.emit(self.model.rows[selected.row()]['batch_id'])
