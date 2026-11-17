"""One actual group at a time; copy uses the selected saved body verbatim."""
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal, QTimer
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton,
                               QPlainTextEdit, QSpinBox, QTableView, QVBoxLayout, QWidget)

from .group_index import ResultStore


class SourceRecordsModel(QAbstractTableModel):
    HEADERS = ("原始编号", "来源套", "号码", "原始金额（元）")
    KEYS = ("item_id", "set_no", "code", "amount")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.records = []

    def set_records(self, records):
        self.beginResetModel()
        self.records = list(records)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.records)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.KEYS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role == Qt.ItemDataRole.DisplayRole:
            return str(self.records[index.row()][self.KEYS[index.column()]])
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section] if orientation == Qt.Orientation.Horizontal else str(section + 1)
        return None


class ResultsWidget(QWidget):
    error_occurred = Signal(str)
    group_changed = Signal(int)
    export_word_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.store = None
        self._copy_serial = 0
        self.group_spin = QSpinBox()
        self.group_spin.setRange(1, 1)
        self.group_spin.setPrefix("第 ")
        self.group_spin.setSuffix(" 组")
        self.group_spin.setKeyboardTracking(False)
        self.group_spin.setObjectName("amount-result-group")
        self.previous_button = QPushButton("上一组")
        self.next_button = QPushButton("下一组")
        self.copy_button = QPushButton("复制本组正文")
        self.copy_button.setObjectName("amount-copy-group")
        self.copy_button.setEnabled(False)
        self.export_word_button = QPushButton("导出本批全部分组为Word")
        self.export_word_button.setObjectName("amount-export-all-word")
        self.export_word_button.setEnabled(False)
        navigation = QHBoxLayout()
        navigation.addWidget(self.previous_button)
        navigation.addWidget(self.group_spin)
        navigation.addWidget(self.next_button)
        navigation.addStretch()
        navigation.addWidget(self.copy_button)
        navigation.addWidget(self.export_word_button)
        self.summary_label = QLabel("选择一个已完成并核验通过的批次")
        self.summary_label.setWordWrap(True)
        self.body_edit = QPlainTextEdit()
        self.body_edit.setReadOnly(True)
        self.body_edit.setObjectName("amount-result-body")
        self.source_model = SourceRecordsModel(self)
        self.source_view = QTableView()
        self.source_view.setModel(self.source_model)
        self.source_view.horizontalHeader().setStretchLastSection(True)
        layout = QVBoxLayout(self)
        layout.addLayout(navigation)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.body_edit, 2)
        layout.addWidget(QLabel("本组原始来源（合并前2～5条）"))
        layout.addWidget(self.source_view, 1)
        self.previous_button.clicked.connect(lambda: self.group_spin.setValue(self.group_spin.value() - 1))
        self.next_button.clicked.connect(lambda: self.group_spin.setValue(self.group_spin.value() + 1))
        self.group_spin.valueChanged.connect(self._show_group)
        self.copy_button.clicked.connect(self.copy_current_body)
        self.export_word_button.clicked.connect(self.export_word_requested)

    def open_batch(self, catalog, batch_id, store=None):
        self.set_store(store or ResultStore(catalog, batch_id))

    def set_store(self, store):
        self._copy_serial += 1
        self.store = store
        self.export_word_button.setEnabled(True)
        self.group_spin.blockSignals(True)
        self.group_spin.setRange(1, store.group_count)
        self.group_spin.setValue(1)
        self.group_spin.blockSignals(False)
        self._show_group(1)

    def clear_store(self, message='请选择已完成且最近完整核查通过的批次'):
        self._copy_serial += 1
        self.store = None
        self.export_word_button.setEnabled(False)
        self.body_edit.clear()
        self.source_model.set_records([])
        self.group_spin.blockSignals(True)
        self.group_spin.setRange(1, 1)
        self.group_spin.blockSignals(False)
        self.copy_button.setEnabled(False)
        self.previous_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.summary_label.setText(message)

    def _show_group(self, group):
        if self.store is None:
            return None
        try:
            result = self.store.get_group(group)
            self.body_edit.setPlainText(result["body"])
            self.source_model.set_records(result["raw_records"])
            self.summary_label.setText(f"第{group}组 / 共{self.store.group_count}组　原始{result['raw_count']}条　"
                                       f"合并后{result['distinct_count']}个号码　组金额{result['total_amount']}元")
            self.copy_button.setEnabled(True)
            self.previous_button.setEnabled(group > 1)
            self.next_button.setEnabled(group < self.store.group_count)
            self.group_changed.emit(group)
            return result
        except Exception as error:
            self.body_edit.clear()
            self.source_model.set_records([])
            self.copy_button.setEnabled(False)
            self.summary_label.setText("结果无法取用：" + str(error))
            self.error_occurred.emit(str(error))
            return None

    def copy_current_body(self):
        # Re-read the selected group's actual bytes at copy time, rather than
        # copying a stale editor value left over from another selection.
        self._copy_serial += 1
        serial, store, group = self._copy_serial, self.store, self.group_spin.value()
        def attempt(number=0):
            if serial != self._copy_serial or store is not self.store or group != self.group_spin.value():
                return None
            result = self._show_group(group)
            if result is None:
                return None
            clipboard = QApplication.clipboard()
            clipboard.setText(result['body'])
            if clipboard.text() == result['body']:
                self.summary_label.setText(self.summary_label.text() + '　已复制本组正文')
                return result['body']
            delays = (50, 100, 200, 400)
            if number < len(delays):
                self.summary_label.setText(self.summary_label.text() + '　剪贴板忙，正在重试')
                QTimer.singleShot(delays[number], lambda: attempt(number + 1))
            else:
                self.summary_label.setText(self.summary_label.text() + '　复制未完成，剪贴板忙，请重试')
                self.error_occurred.emit('剪贴板忙，未能确认复制，请重试')
            return None
        return attempt()
