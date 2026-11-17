from types import SimpleNamespace
from number_app.amount_mode.ui import results as module


def test_clipboard_busy_retries_are_bounded_and_never_copy_a_new_selection(qtbot, monkeypatch):
    class Store:
        group_count = 2
        def get_group(self, group):
            return {'body': f'{group:03d}直选1元\n', 'raw_records': [], 'raw_count': 2, 'distinct_count': 1, 'total_amount': '1'}
    class Clipboard:
        value = 'sentinel'
        failures = 1
        calls = 0
        def setText(self, value):
            self.calls += 1
            if self.failures: self.failures -= 1
            else: self.value = value
        def text(self): return self.value
    clipboard = Clipboard()
    monkeypatch.setattr(module, 'QApplication', SimpleNamespace(clipboard=lambda: clipboard))
    widget = module.ResultsWidget(); qtbot.addWidget(widget)
    widget.set_store(Store()); widget.copy_button.click()
    qtbot.waitUntil(lambda: clipboard.text() == '001直选1元\n', timeout=1500)
    assert clipboard.calls == 2 and '已复制' in widget.summary_label.text()
    clipboard.failures = 100; clipboard.value = 'new-sentinel'
    widget.copy_button.click(); calls = clipboard.calls
    widget.group_spin.setValue(2)
    qtbot.wait(850)
    assert clipboard.value == 'new-sentinel' and clipboard.calls == calls


def test_failed_incomplete_recovery_keeps_failure_and_original_anchor(tmp_path):
    from number_app.amount_mode.catalog import Catalog
    from number_app.amount_mode.spec import AmountBatchSpec
    from number_app.amount_mode.input_config import build_request_bytes
    from number_app.amount_mode.recovery import recover_amount_batch
    from number_app.amount_mode.service import AuditFailed
    from number_app.storage.locking import WorkspaceLock
    import pytest
    with WorkspaceLock(tmp_path):
        catalog = Catalog(tmp_path)
        catalog.create(AmountBatchSpec((1,)), build_request_bytes(AmountBatchSpec((1,)), batch_id='failed_qa'))
        catalog.transition('failed_qa', 'GENERATING'); catalog.transition('failed_qa', 'WRITING')
        catalog.transition('failed_qa', 'FAILED', error='QA disk full')
    before = catalog.get('failed_qa')
    with pytest.raises(AuditFailed, match='缺少首次可信快照'):
        recover_amount_batch(tmp_path, 'failed_qa')
    assert catalog.get('failed_qa') == before
