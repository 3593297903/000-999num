from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit
from number_app.amount_mode.ui.app import AmountWindow
from number_app.amount_mode.catalog import Catalog
import pytest


@pytest.mark.parametrize('amounts', [(1,), (1, 5, 2)])
def test_daily_gui_visible_editor_generate_copy_history_reopen_verify(qtbot, tmp_path, amounts):
    root = tmp_path / 'gui'
    window = AmountWindow(root); qtbot.addWidget(window); window.show()
    new = window.new_page
    new.count_input.setValue(len(amounts))
    for row, value in enumerate(amounts):
        new.model.setData(new.model.index(row, 1), str(value), Qt.EditRole)
    expected = sum(amounts)
    assert str(expected) + '元' in new.summary_label.text()
    if len(amounts) == 3:
        index = new.model.index(2, 1)
        new.table.setCurrentIndex(index); new.table.edit(index)
        editor = new.table.findChild(QLineEdit)
        assert editor is not None
        editor.selectAll(); qtbot.keyClicks(editor, '9')
        assert new.model.amount_strings() == ('1', '5', '2')
        expected = 15
    qtbot.mouseClick(new.start_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: not window.busy, timeout=120000)
    assert window.catalog.count('COMPLETED') == 1
    bid = window.catalog.list_page()[0]['batch_id']
    assert window.catalog.get(bid)['per_code_expected_amount'] == str(expected)
    assert all('PASS' in label.text() for label in window.check_labels.values())
    assert window.stats_page.table_model.rowCount() == 1000
    body = window.results_page.body_edit.toPlainText()
    qtbot.mouseClick(window.results_page.copy_button, Qt.LeftButton)
    assert QApplication.clipboard().text().rstrip('\n') == body.rstrip('\n')
    old = window.stage_label.text()
    window._on_progress({'token': 'stale-task', 'event': {'batch_id': bid, 'phase': 'check_finished', 'method': 'A', 'round': 1, 'status': 'FAIL'}})
    assert window.stage_label.text() == old
    original_audit = window.catalog.get(bid)['latest_audit_id']
    window.close(); window.deleteLater(); qtbot.wait(20)
    reopened = AmountWindow(root); qtbot.addWidget(reopened); reopened.show()
    assert reopened.history_page.model.rowCount() == 1
    reopened.open_batch(bid)
    qtbot.waitUntil(lambda: not reopened.busy, timeout=30000)
    assert reopened.results_page.body_edit.toPlainText() == body
    reopened.verify_selected()
    qtbot.waitUntil(lambda: not reopened.busy, timeout=120000)
    assert Catalog(root).get(bid)['latest_verification_status'] == 'PASS'
    assert Catalog(root).get(bid)['latest_audit_id'] != original_audit
    reopened.close()


def test_bad_catalog_diagnostic_does_not_recreate(qtbot, tmp_path):
    root = tmp_path / 'bad'; root.mkdir()
    p = root / 'history.sqlite'; p.write_bytes(b'not sqlite')
    window = AmountWindow(root); qtbot.addWidget(window); window.show()
    assert window.catalog is None and not window.new_page.start_button.isEnabled()
    assert p.read_bytes() == b'not sqlite'
    assert not (root / 'logs').exists()
    window.close()
