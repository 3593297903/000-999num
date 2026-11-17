from PySide6.QtWidgets import QApplication
from number_app.amount_mode.service import generate_amount_batch
from number_app.amount_mode.catalog import Catalog
from number_app.amount_mode.ui.app import AmountWindow


def test_invalid_or_loading_selection_cannot_copy_previous_batch(qtbot, tmp_path):
    root = tmp_path / 'selection'
    a = generate_amount_batch((1,), root)
    b = generate_amount_batch((1, 5, 2), root)
    window = AmountWindow(root); qtbot.addWidget(window); window.show()
    window.open_batch(a['batch_id'])
    qtbot.waitUntil(lambda: not window.busy, timeout=30000)
    assert window.results_page.copy_button.isEnabled()
    Catalog(root).mark_verification(b['batch_id'], 'FAIL', error='QA selection state')
    QApplication.clipboard().setText('QA-sentinel')
    window.open_batch(b['batch_id'])
    assert window._selected_bid == b['batch_id']
    assert window.results_page.store is None and not window.results_page.copy_button.isEnabled()
    assert window.results_page.body_edit.toPlainText() == ''
    assert window.stats_page.table_model.rowCount() == 0
    window.results_page.copy_button.click()
    assert QApplication.clipboard().text() == 'QA-sentinel'
    window.open_batch(a['batch_id'])
    assert window.busy and not window.history_page.isEnabled()
    assert window.results_page.store is None and not window.results_page.copy_button.isEnabled()
    qtbot.waitUntil(lambda: not window.busy, timeout=30000)
    assert window.results_page.store.batch_id == a['batch_id']
    assert window.history_page.isEnabled() and window.results_page.copy_button.isEnabled()
    window.close()
