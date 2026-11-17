"""Set-amount input view; submit the visible editor before freezing a batch."""
from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QSignalBlocker, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemDelegate, QAbstractItemView, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QSpinBox, QStyledItemDelegate, QTableView, QVBoxLayout, QWidget,
)

from ..limits import MAX_SETS
from .input_model import AmountInputModel


class _AmountDelegate(QStyledItemDelegate):
    visible_text_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._serial = 0
        self._active = None

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        editor.setObjectName("amount_editor")
        editor.setPlaceholderText("1及以上整数，最多100位")
        # Do not install a validator or a 100-character truncation limit: invalid
        # text remains visible as a draft and is explicitly refused at start.
        self._serial += 1
        token = self._serial
        self._active = (token, editor, QPersistentModelIndex(index))
        editor.textChanged.connect(lambda _value: self.visible_text_changed.emit())
        editor.destroyed.connect(lambda _=None, key=token: self._forget(key))
        return editor

    def _forget(self, token):
        if self._active is not None and self._active[0] == token:
            self._active = None
            self.visible_text_changed.emit()

    def setEditorData(self, editor, index):
        editor.setText(index.data(Qt.ItemDataRole.EditRole))
        editor.selectAll()

    def setModelData(self, editor, model, index):
        model.setData(index, editor.text(), Qt.ItemDataRole.EditRole)

    def overlay(self, values):
        result = list(values)
        if self._active is not None:
            _, editor, index = self._active
            if index.isValid() and 0 <= index.row() < len(result):
                try:
                    result[index.row()] = editor.text()
                except RuntimeError:
                    self._active = None
        return tuple(result)

    def commit_active(self, model):
        if self._active is None:
            return
        token, editor, index = self._active
        if index.isValid():
            # Synchronous on the owning GUI thread; no focus-change assumption.
            self.setModelData(editor, model, QModelIndex(index))
            self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.NoHint)
        self._forget(token)


class NewBatchWidget(QWidget):
    start_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False
        self._freezing = False
        self._frozen_spec = None
        self.model = AmountInputModel(self)
        self.table = QTableView(self)
        self.table.setObjectName("amount_table")
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.SelectedClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._delegate = _AmountDelegate(self.table)
        self.table.setItemDelegateForColumn(1, self._delegate)
        self.count_input = QSpinBox(self)
        self.count_input.setObjectName("set_count_input")
        self.count_input.setRange(1, MAX_SETS)
        self.count_input.setValue(1)
        self.count_input.setSuffix(" 套")
        self.summary_label = QLabel(self)
        self.summary_label.setObjectName("input_summary")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.start_button = QPushButton("开始生成并核查15次", self)
        self.start_button.setObjectName("start_amount_batch")
        self.error_label = QLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b42318;")
        top = QHBoxLayout()
        top.addWidget(QLabel("套数：", self))
        top.addWidget(self.count_input)
        top.addWidget(QLabel(f"支持1～{MAX_SETS}套；每套完整000～999", self))
        top.addStretch()
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(QLabel("逐套填写统一金额；每组随机分2～5条原始记录，同组同号金额相加。", self))
        layout.addWidget(self.table, 1)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.error_label)
        layout.addWidget(self.start_button)
        self.count_input.valueChanged.connect(self._change_count)
        self.model.draft_changed.connect(self._refresh_summary)
        self._delegate.visible_text_changed.connect(self._refresh_summary)
        self.start_button.clicked.connect(self._request_start)
        self._refresh_summary()

    @property
    def frozen_spec(self):
        return self._frozen_spec

    @property
    def busy(self):
        return self._busy

    def _gui_thread(self):
        if QThread.currentThread() != self.thread():
            raise RuntimeError("开始和冻结输入必须在GUI线程执行")

    def _change_count(self, count):
        self._gui_thread()
        self._delegate.commit_active(self.model)
        self.model.set_set_count(count)

    def _refresh_summary(self):
        if self.count_input.value() != self.model.set_count:
            with QSignalBlocker(self.count_input):
                self.count_input.setValue(self.model.set_count)
        if self._busy and self._frozen_spec is not None:
            spec = self._frozen_spec
            self.summary_label.setText(f"本批输入已固定\n已填写：{spec.set_count} / {spec.set_count} 套\n"
                                       f"每套金额合计：{spec.per_code_amount}元\n原始记录总数：{spec.raw_total}条\n"
                                       f"全部号码总金额：{spec.total_amount}元")
            self.start_button.setEnabled(False)
            return
        summary = self.model.summarize(self._delegate.overlay(self.model.amount_strings()))
        lines = [f"已填写：{summary['valid_count']} / {summary['set_count']} 套"]
        if summary["complete"]:
            lines += [f"每套金额合计：{summary['subtotal']}元", f"原始记录总数：{summary['raw_total']}条",
                      f"全部号码总金额：{summary['subtotal'] * 1000}元"]
        else:
            lines += [f"当前有效金额小计：{summary['subtotal']}元（不是完整合计）",
                      f"原始记录总数：{summary['raw_total']}条",
                      "待填写或非法套号：" + "、".join(map(str, summary["invalid_sets"]))]
        self.summary_label.setText("\n".join(lines))
        self.start_button.setEnabled(summary["complete"] and not self._busy and not self._freezing)

    def freeze_spec(self):
        """Commit visible text and copy strict values without processing Qt events."""
        self._gui_thread()
        if self._busy:
            raise ValueError("本批正在运行，不能重复开始")
        self._delegate.commit_active(self.model)
        self.count_input.interpretText()
        self.model.set_set_count(self.count_input.value())
        # This reads all current rows once; neither the label nor a Qt object is
        # included in the returned frozen dataclass/tuple.
        spec = self.model.freeze_spec()
        self._refresh_summary()
        return spec

    def _request_start(self):
        if self._busy or self._freezing:
            return
        self._freezing = True
        self.start_button.setEnabled(False)
        try:
            spec = self.freeze_spec()
        except (ValueError, RuntimeError) as error:
            self.error_label.setText(str(error))
            return
        finally:
            self._freezing = False
            self._refresh_summary()
        self.error_label.clear()
        self._frozen_spec = spec
        self.set_busy(True)
        self.start_requested.emit(spec)

    def set_busy(self, busy):
        self._gui_thread()
        self._busy = bool(busy)
        self.count_input.setEnabled(not self._busy)
        self.table.setEnabled(not self._busy)
        self._refresh_summary()
