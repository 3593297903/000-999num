"""Daily desktop surface; every job uses the same coordinator as the CLI."""
from pathlib import Path
from datetime import datetime
import json
import os
import threading
import time
import uuid

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QTabWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QPushButton, QLineEdit, QFileDialog, QMessageBox, QGridLayout, QPlainTextEdit)

from number_app.storage.locking import WorkspaceLock
from ..catalog import Catalog
from ..protocol import canonical_json, strict_json, synced_write
from ..runtime import prepare_runtime, configure_logging, log_event, save_draft, precheck_catalog
from ..output_paths import protect_paths
from ..service import generate_amount_batch
from ..recovery import verify_amount_batch, recover_amount_batch
from .new_batch import NewBatchWidget
from .history import HistoryWidget
from .results import ResultsWidget
from .statistics import StatisticsWidget
from .group_index import ResultStore


class _Job(QObject):
    progress = Signal(object)
    done = Signal(object)
    ended = Signal()
    def __init__(self, token, function):
        super().__init__(); self.token = token; self.function = function
    @Slot()
    def run(self):
        try:
            value = self.function(lambda event: self.progress.emit({'token': self.token, 'event': event}))
            self.done.emit({'token': self.token, 'ok': True, 'value': value})
        except BaseException as error:
            self.done.emit({'token': self.token, 'ok': False, 'error': str(error), 'type': type(error).__name__})
        finally:
            self.ended.emit()


class AmountWindow(QMainWindow):
    def __init__(self, data_root='E:/number/data_amount', parent=None):
        super().__init__(parent)
        self.setWindowTitle('按套金额随机小组 1.1')
        self.resize(1080, 760)
        self.catalog = None; self.paths = None; self.logger = None
        self._thread = None; self._worker = None; self._token = None; self._job_kind = None
        self._pending_done = None; self._cancel = threading.Event(); self._closing = False
        self._selected_bid = None; self._current_bid = None; self._audit_id = None
        self._started = time.monotonic(); self._heartbeat_count = 0; self._finished_jobs = 0
        self.tabs = QTabWidget(); self.setCentralWidget(self.tabs)
        self.new_page = NewBatchWidget(); self.new_page.start_requested.connect(self.start_generation)
        self.tabs.addTab(self.new_page, '新建')
        self.progress_page = QWidget(); progress_layout = QVBoxLayout(self.progress_page)
        self.stage_label = QLabel('就绪'); progress_layout.addWidget(self.stage_label)
        grid = QGridLayout(); self.check_labels = {}
        for row, method in enumerate('ABC'):
            grid.addWidget(QLabel(method), row, 0)
            for number in range(1, 6):
                label = QLabel(f'{method}{number} 待执行'); grid.addWidget(label, row, number)
                self.check_labels[(method, number)] = label
        progress_layout.addLayout(grid)
        self.cancel_button = QPushButton('取消当前任务（提交开始后等待完成）'); self.cancel_button.clicked.connect(self.cancel_job)
        progress_layout.addWidget(self.cancel_button)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True); self.details.document().setMaximumBlockCount(200)
        progress_layout.addWidget(self.details); self.tabs.addTab(self.progress_page, '进度')
        self.results_page = ResultsWidget()
        self.results_page.export_word_requested.connect(self.export_word_selected)
        self.tabs.addTab(self.results_page, '分组结果')
        self.stats_page = StatisticsWidget(); self.tabs.addTab(self.stats_page, '逐号统计')
        self.history_page = HistoryWidget(); self.history_page.batch_selected.connect(self.open_batch)
        self.tabs.addTab(self.history_page, '历史')
        self.tools_page = QWidget(); tools = QVBoxLayout(self.tools_page)
        tools.addWidget(QLabel('复核读取固定结果并重新执行15次，不重新分组。外部声明自洽会单独标明来源。'))
        self.data_edit = QLineEdit(str(data_root)); self.backup_edit = QLineEdit()
        tools.addWidget(QLabel('数据目录')); tools.addWidget(self.data_edit)
        tools.addWidget(QLabel('逐批备份目录')); tools.addWidget(self.backup_edit)
        self.actions = []
        for text, function in [('应用目录设置', self.apply_settings), ('独立复核所选批次', self.verify_selected),
            ('选择外部完整批次复核', self.verify_external), ('恢复所选中断批次', self.recover_selected),
            ('导出所选批次', self.export_selected), ('导出所选批次全部分组为Word', self.export_word_selected),
            ('备份所选批次', self.backup_selected),
            ('从自包含备份恢复到新目录', self.restore_package), ('保存历史索引快照', self.snapshot_catalog)]:
            button = QPushButton(text); button.clicked.connect(function); tools.addWidget(button); self.actions.append(button)
        tools.addStretch(); self.tabs.addTab(self.tools_page, '复核、备份与设置')
        self.status_label = QLabel(); self.statusBar().addPermanentWidget(self.status_label)
        self.backup_warning = QLabel(); self.backup_warning.setStyleSheet('color: #b42020')
        self.statusBar().addWidget(self.backup_warning)
        self._initialize_workspace(data_root)
        self.timer = QTimer(self); self.timer.timeout.connect(self._heartbeat); self.timer.start(1000)

    @property
    def busy(self): return self._thread is not None

    def _initialize_workspace(self, data_root, backup_root=None):
        self._selected_bid = None
        self.results_page.clear_store()
        self.stats_page.clear_store()
        try:
            paths = prepare_runtime(data_root, backup_root)
            precheck_catalog(paths['root'])
            with WorkspaceLock(paths['root']): catalog = Catalog(paths['root'])
            self.catalog = catalog; self.paths = paths; self.logger = configure_logging(paths)
            self.data_edit.setText(str(paths['root'])); self.backup_edit.setText(str(paths['backup']))
            self.history_page.set_catalog(catalog)
            draft = paths['drafts'] / 'input.json'
            if draft.exists() and draft.stat().st_size <= 60000:
                self.new_page.model.restore_draft(strict_json(draft.read_bytes()))
                self.new_page.count_input.setValue(self.new_page.model.rowCount())
            self.stage_label.setText('就绪；每批15次全部通过后开放结果')
            log_event(self.logger, {'event': 'gui_started', 'pid': os.getpid(), 'data_root': str(paths['root'])})
        except Exception as error:
            self.catalog = None
            self.history_page.catalog = None
            self.history_page.model.replace_rows([])
            self.stage_label.setText('目录/历史库不可用，保留原文件：' + str(error))
            self.details.appendPlainText(str(error) + '\n可在设置中选择新目录，或从可信自包含包独立恢复。')
        self.new_page.set_busy(self.catalog is None)

    def apply_settings(self):
        if self.busy: return
        self._initialize_workspace(self.data_edit.text(), self.backup_edit.text() or None)

    def _start_job(self, kind, function):
        if self.busy: return
        self._token = uuid.uuid4().hex; self._job_kind = kind; self._pending_done = None
        self._cancel.clear(); self._current_bid = None; self._audit_id = None
        self.new_page.set_busy(True)
        self.history_page.setEnabled(False)
        self.results_page.setEnabled(False)
        for button in self.actions: button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        if kind != 'load':
            for (method, number), label in self.check_labels.items():
                label.setText(f'{method}{number} 待执行'); label.setStyleSheet('')
            self.tabs.setCurrentWidget(self.progress_page)
        self._thread = QThread(self)
        self._worker = _Job(self._token, function); self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_done)
        self._worker.ended.connect(self._thread.quit)
        self._worker.ended.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread_finished)
        self._thread.start()

    def start_generation(self, spec):
        if self.catalog is None or self.busy: return
        try:
            save_draft(self.paths, self.new_page.model.draft_state())
        except Exception as error:
            self.new_page.set_busy(False); self.stage_label.setText('草稿保存失败：' + str(error)); return
        root, backup = self.paths['root'], self.paths['backup']
        self._start_job('generate', lambda progress: generate_amount_batch(spec, root, backup_root=backup, cancel=self._cancel.is_set, progress=progress))

    def _on_progress(self, envelope):
        if envelope.get('token') != self._token: return
        event = envelope['event']; bid = event.get('batch_id')
        if self._current_bid is None: self._current_bid = bid
        if bid != self._current_bid: return
        phase = event.get('phase', event.get('state', ''))
        if phase == 'audit_started': self._audit_id = event.get('audit_id')
        if event.get('audit_id') and self._audit_id and event['audit_id'] != self._audit_id: return
        log_event(self.logger, {'event': 'job_progress', **event})
        self.stage_label.setText(f'{bid}  {phase}')
        if phase in ('check_started', 'check_finished'):
            label = self.check_labels.get((event['method'], event['round']))
            if label:
                status = event.get('status', '执行中'); label.setText(f"{event['method']}{event['round']} {status}")
                label.setStyleSheet('color: #087a35' if status == 'PASS' else '')
        self.details.appendPlainText(f"{datetime.now():%H:%M:%S} {phase} {event.get('method', '')}{event.get('round', '')} {event.get('status', '')}")

    def _on_done(self, envelope):
        if envelope.get('token') == self._token: self._pending_done = envelope

    def _thread_finished(self):
        thread, kind, done = self._thread, self._job_kind, self._pending_done
        self._thread = None; self._worker = None; self._pending_done = None
        if thread: thread.deleteLater()
        self._finished_jobs += 1
        self.new_page.set_busy(self.catalog is None)
        self.history_page.setEnabled(self.catalog is not None)
        self.results_page.setEnabled(True)
        for button in self.actions: button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if self.catalog: self.history_page.refresh()
        if self._closing:
            self.close(); return
        if not done: self.stage_label.setText('任务未返回完整结果'); return
        if not done['ok']:
            if kind in ('load', 'verify', 'recover'):
                self.results_page.clear_store(); self.stats_page.clear_store()
            self.stage_label.setText('任务未完成：' + done['error']); self.details.appendPlainText(done['error']); return
        value = done['value']
        if kind == 'load':
            if value.batch_id != self._selected_bid:
                self.results_page.clear_store(); self.stats_page.clear_store()
                self.stage_label.setText('忽略不属于当前选择的延迟结果'); return
            self.results_page.set_store(value); self.stats_page.set_store(value)
            self.tabs.setCurrentWidget(self.results_page); self.stage_label.setText('已按原指纹读取固定结果')
        else:
            self.stage_label.setText('操作完成：' + str(value.get('status', '')))
            self.details.appendPlainText(json.dumps(value, ensure_ascii=False, default=str))
            if kind == 'generate':
                backup = value.get('backup', {})
                self.backup_warning.setText('本批备份失败：' + backup.get('error', '') if backup.get('status') == 'FAILED' else '')
                self._selected_bid = value['batch_id']
                self.open_batch(value['batch_id'])
            elif kind == 'word_export':
                self.stage_label.setText(f"Word导出完成：共{value['group_count']}组，保存于{value['path']}")
                QMessageBox.information(self, 'Word导出完成',
                    f"已导出本批全部{value['group_count']}组。\n\n保存位置：{value['path']}")
                self.tabs.setCurrentWidget(self.results_page)

    def cancel_job(self):
        self._cancel.set(); self.cancel_button.setEnabled(False)
        self.stage_label.setText('已请求取消；若已进入提交，等待安全提交结束')

    def open_batch(self, bid):
        if self.busy or self.catalog is None: return
        self._selected_bid = bid
        # Never leave a previous batch copyable while the selected one is
        # invalid, being loaded, or fails to load.
        self.results_page.clear_store('正在核对所选批次：' + bid)
        self.stats_page.clear_store()
        row = self.catalog.get(bid)
        if row['generation_status'] != 'COMPLETED' or row['latest_verification_status'] != 'PASS':
            self.stage_label.setText(f"所选批次 {bid}：{row['generation_status']} / {row['latest_verification_status']}；请恢复或重新完整复核")
            return
        self._start_job('load', lambda progress: ResultStore(self.catalog, bid, self.paths['cache']))

    def _selection(self):
        if self._selected_bid is None: raise ValueError('请先在历史中选择批次')
        return self._selected_bid

    def _action(self, kind, function):
        if self.busy: return
        try: function()
        except Exception as error: self.stage_label.setText(str(error))

    def verify_selected(self):
        def action():
            bid = self._selection(); path = self.catalog.path(bid)
            self._start_job('verify', lambda p: verify_amount_batch(path, self.catalog.root, cancel=self._cancel.is_set, progress=p))
        self._action('verify', action)

    def verify_external(self):
        path = QFileDialog.getExistingDirectory(self, '选择含五个固定文件的批次')
        if path and self.catalog: self._start_job('verify', lambda p: verify_amount_batch(path, self.catalog.root, cancel=self._cancel.is_set, progress=p))

    def recover_selected(self):
        def action():
            bid = self._selection()
            self._start_job('recover', lambda p: recover_amount_batch(self.catalog.root, bid, cancel=self._cancel.is_set, progress=p, backup_root=self.paths['backup']))
        self._action('recover', action)

    def export_selected(self):
        def action():
            bid = self._selection(); folder = QFileDialog.getExistingDirectory(self, '选择独立导出目录')
            if folder:
                from ..export import export_amount_batch
                self._start_job('export', lambda p: export_amount_batch(self.catalog.root, bid, folder))
        self._action('export', action)

    def backup_selected(self):
        def action():
            bid = self._selection()
            from ..backup import backup_amount_batch
            self._start_job('backup', lambda p: backup_amount_batch(self.catalog.root, bid, self.paths['backup']))
        self._action('backup', action)

    def export_word_selected(self):
        def action():
            bid = self._selection()
            root = self.catalog.root
            filename, _ = QFileDialog.getSaveFileName(self, '导出本批全部分组为Word',
                f'全部分组_{bid}.docx', 'Word文档 (*.docx)',
                options=QFileDialog.Option.DontConfirmOverwrite)
            if filename:
                if not filename.lower().endswith('.docx'):
                    filename += '.docx'
                from ..word_export import export_amount_word
                self._start_job('word_export', lambda progress: export_amount_word(
                    root, bid, filename, cancel=self._cancel.is_set,
                    progress=lambda event: progress({'batch_id': bid, **event})))
        self._action('word_export', action)

    def restore_package(self):
        package = QFileDialog.getExistingDirectory(self, '选择含BACKUP_COMPLETE.json的完整包')
        if not package: return
        target = QFileDialog.getExistingDirectory(self, '选择全新或空的恢复数据目录')
        if target:
            from ..restore import restore_amount_backup
            self._start_job('restore', lambda p: restore_amount_backup(package, target, cancel=self._cancel.is_set, progress=p))

    def snapshot_catalog(self):
        if not self.catalog: return
        from ..backup import snapshot_amount_catalog
        self._start_job('snapshot', lambda p: snapshot_amount_catalog(self.catalog.root, self.paths['backup']))

    def _heartbeat(self):
        self._heartbeat_count += 1
        self.status_label.setText(f"{'处理中' if self.busy else '就绪'} · {datetime.now():%H:%M:%S}")
        if self.paths and self._heartbeat_count % 5 == 0:
            health = {'pid': os.getpid(), 'started_monotonic': self._started, 'uptime_seconds': time.monotonic() - self._started,
                      'heartbeat_count': self._heartbeat_count, 'busy': self.busy, 'finished_jobs': self._finished_jobs,
                      'batch_id': self._current_bid, 'at': datetime.now().astimezone().isoformat()}
            file = self.paths['logs'] / 'gui_health.json'
            try:
                file, = protect_paths(file, workspace_roots=(self.paths['root'],), file_paths=(file,))
                synced_write(file, canonical_json(health), exclusive=False)
            except Exception as error: log_event(self.logger, {'event': 'health_write_failed', 'error': str(error)})

    def closeEvent(self, event):
        if self.busy:
            self._closing = True; self.cancel_job(); event.ignore(); return
        if self.paths and self.catalog:
            try: save_draft(self.paths, self.new_page.model.draft_state())
            except Exception as error: log_event(self.logger, {'event': 'draft_write_failed', 'error': str(error)})
        log_event(self.logger, {'event': 'gui_closed', 'pid': os.getpid(), 'uptime_seconds': time.monotonic() - self._started})
        if self.logger:
            for handler in self.logger.handlers[:]: handler.close(); self.logger.removeHandler(handler)
        event.accept()


def run_gui(data_root='E:/number/data_amount', *, start_in_verify=False):
    app = QApplication.instance() or QApplication([])
    app.setApplicationName('按套金额随机小组')
    window = AmountWindow(data_root)
    if start_in_verify: window.tabs.setCurrentWidget(window.tools_page)
    window.show()
    return app.exec()
