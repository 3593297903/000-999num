"""Real Qt editing and immutable input capture, without generating test batches."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit

from number_app.amount_mode.ui.input_model import AmountInputModel
from number_app.amount_mode.ui.new_batch import NewBatchWidget


def make_widget(qtbot):
    widget = NewBatchWidget()
    qtbot.addWidget(widget)
    widget.resize(760, 480)
    widget.show()
    qtbot.waitUntil(widget.isVisible)
    return widget


def fill(widget, values):
    widget.count_input.setValue(len(values))
    for row, value in enumerate(values):
        assert widget.model.setData(widget.model.index(row, 1), value)


def edit_visible(qtbot, widget, row, value):
    index = widget.model.index(row, 1)
    widget.table.setCurrentIndex(index)
    widget.table.edit(index)
    editors = widget.table.findChildren(QLineEdit, "amount_editor")
    editor = next(editor for editor in editors if editor.isVisible())
    qtbot.keyClick(editor, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClicks(editor, value)
    return editor


def test_last_visible_editor_is_committed_before_real_start_click(qtbot):
    widget = make_widget(qtbot)
    fill(widget, ["1", "5", "2"])
    assert "每套金额合计：8元" in widget.summary_label.text()
    assert "原始记录总数：3000条" in widget.summary_label.text()
    assert "全部号码总金额：8000元" in widget.summary_label.text()
    editor = edit_visible(qtbot, widget, 2, "9")
    assert editor.text() == "9"
    assert widget.model.amount_strings() == ("1", "5", "2")
    assert "每套金额合计：15元" in widget.summary_label.text()
    captured = []
    widget.start_requested.connect(captured.append)
    qtbot.mouseClick(widget.start_button, Qt.MouseButton.LeftButton)
    assert len(captured) == 1
    spec = captured[0]
    assert spec.amounts == (1, 5, 9)
    assert spec.per_code_amount == 15 and spec.total_amount == 15000
    assert widget.model.amount_strings() == ("1", "5", "9")
    assert widget.busy and not widget.start_button.isEnabled()
    assert not widget.table.isEnabled() and not widget.count_input.isEnabled()
    qtbot.mouseClick(widget.start_button, Qt.MouseButton.LeftButton)
    widget._request_start()
    assert len(captured) == 1
    # A subsequent draft edit cannot mutate the object already sent to a worker.
    widget.model.setData(widget.model.index(2, 1), "2")
    assert spec.amounts == (1, 5, 9) and widget.frozen_spec is spec
    assert "每套金额合计：15元" in widget.summary_label.text()
    widget.set_busy(False)
    assert "每套金额合计：8元" in widget.summary_label.text()


def test_partial_and_invalid_drafts_never_become_batch_inputs(qtbot):
    widget = make_widget(qtbot)
    assert not widget.start_button.isEnabled()
    assert "已填写：0 / 1 套" in widget.summary_label.text()
    fill(widget, ["1", "", "5"])
    text = widget.summary_label.text()
    assert "已填写：2 / 3 套" in text and "当前有效金额小计：6元" in text
    assert "不是完整合计" in text and "待填写或非法套号：2" in text
    assert not widget.start_button.isEnabled()
    for invalid in ("0", "-1", "1.5", "1e3", " 1 ", "１", "9" * 101):
        widget.model.setData(widget.model.index(1, 1), invalid)
        assert not widget.start_button.isEnabled()
        with pytest.raises(ValueError):
            widget.freeze_spec()
    # Realtime preview must also reject uncommitted visible text over a valid model.
    fill(widget, ["1"])
    edit_visible(qtbot, widget, 0, "0")
    assert widget.model.amount_strings() == ("1",)
    assert not widget.start_button.isEnabled()
    with pytest.raises(ValueError):
        widget.freeze_spec()
    assert widget.model.amount_strings() == ("0",)


def test_reducing_and_reexpanding_preserves_all_draft_rows(qtbot):
    widget = make_widget(qtbot)
    fill(widget, ["1", "5", "2"])
    widget.count_input.setValue(1)
    saved = widget.model.draft_state()
    assert saved == {"set_count": 1, "amounts": ["1", "5", "2"]}
    saved["amounts"][0] = "99"
    assert widget.model.amount_strings() == ("1",)
    widget.count_input.setValue(3)
    assert widget.model.amount_strings() == ("1", "5", "2")
    widget.count_input.setValue(4)
    assert widget.model.amount_strings() == ("1", "5", "2", "")
    assert "已填写：3 / 4 套" in widget.summary_label.text()
    assert "当前有效金额小计：8元" in widget.summary_label.text()
    clone = AmountInputModel(widget)
    clone.restore_draft({"set_count": 1, "amounts": ["1", "5", "2"]})
    clone.set_set_count(3)
    assert clone.amount_strings() == ("1", "5", "2")
    for invalid_count in (0, 301, True):
        with pytest.raises(ValueError):
            clone.set_set_count(invalid_count)
    assert not clone.setData(clone.index(0, 1), 1)


def test_direct_freeze_submits_editor_and_keeps_large_integers_exact(qtbot):
    widget = make_widget(qtbot)
    large = "100000000000000003"
    fill(widget, [large, "5"])
    edit_visible(qtbot, widget, 1, "9")
    assert widget.model.amount_strings() == (large, "5")
    # There is no focus change, Enter key, or button click before this capture.
    spec = widget.freeze_spec()
    assert spec.amounts == (100000000000000003, 9)
    assert spec.per_code_amount == 100000000000000012
    assert spec.total_amount == 100000000000000012000
    widget.model.setData(widget.model.index(0, 1), "1")
    assert spec.amounts == (100000000000000003, 9)
    maximum = "9" * 100
    fill(widget, [maximum, maximum])
    spec = widget.freeze_spec()
    assert spec.per_code_amount == 2 * int(maximum)
    assert len(str(spec.per_code_amount)) == 101
    assert str(spec.total_amount) in widget.summary_label.text()
