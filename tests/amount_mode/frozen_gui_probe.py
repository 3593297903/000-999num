"""External QA target; top-level imports are stdlib only, never bundled."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

_QT_MESSAGES = []
_CLIPBOARD_RETRIES = []


def _require(value, message):
    if not value:
        raise ValueError(message)


def _write(path, value):
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundled_imports():
    _require(getattr(sys, "frozen", False) is True and hasattr(sys, "_MEIPASS"), "QA target is not running in frozen exe")
    bundle = Path(sys._MEIPASS).resolve(strict=True)
    prefix = [str(bundle), str(bundle / "base_library.zip")]
    sys.path[:] = prefix + [path for path in sys.path if path not in prefix]
    if hasattr(os, "add_dll_directory"):
        # Keep handles alive for the complete GUI lifetime.
        handles = [os.add_dll_directory(str(path)) for path in (bundle, bundle / "PySide6") if path.is_dir()]
    else:
        handles = []
    plugins = bundle / "PySide6" / "plugins" / "platforms"
    if plugins.is_dir(): os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(plugins)
    names = ("PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
             "number_app.amount_mode.ui.app", "number_app.amount_mode.ui.new_batch",
             "number_app.amount_mode.ui.group_index", "number_app.amount_mode.ui.results",
             "number_app.amount_mode.ui.statistics", "number_app.amount_mode.service")
    origins = {}
    for name in names:
        module = importlib.import_module(name)
        origin = Path(module.__file__).resolve(strict=False)
        _require(origin.is_relative_to(bundle), "Source fallback forbidden: " + name + " -> " + str(origin))
        origins[name] = str(origin)
    return bundle, origins, handles


def _wait(app, predicate, description, window=None, seconds=240):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate(): return
        time.sleep(0.01)
    detail = "" if window is None else window.stage_label.text() + "\n" + window.details.toPlainText()
    raise TimeoutError(description + "\n" + detail)


def _button(parent, text):
    from PySide6.QtWidgets import QPushButton
    found = [item for item in parent.findChildren(QPushButton) if item.text() == text]
    _require(len(found) == 1, "actual button not unique: " + text)
    return found[0]


def _editor(app, new, row, value, commit=False):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication, QLineEdit
    index = new.model.index(row, 1)
    new.table.setCurrentIndex(index); new.table.edit(index); app.processEvents()
    editor = next((item for item in new.table.findChildren(QLineEdit, "amount_editor") if item.isVisible()), None)
    _require(editor is not None, "actual delegate editor did not open")
    editor.setText(str(value))
    if commit:
        QApplication.sendEvent(editor, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier, "\r"))
        app.processEvents()
        _require(new.model.amount_strings()[row] == str(value), "actual editor Enter did not submit model")
    return editor


def _fixed(batch):
    return {name: _hash(batch / name) for name in ("request.json", "group_plan.json", "items.jsonl", "groups.txt", "manifest.json")}


def _audit(window, bid, amounts, bundle):
    from number_app.amount_mode.protocol import COMMON_PROOFS, EXTRA_PROOFS
    row = window.catalog.get(bid)
    batch = window.catalog.path(bid)
    n, total = len(amounts) * 1000, sum(amounts) * 1000
    _require(row["generation_status"] == "COMPLETED" and row["latest_verification_status"] == "PASS", "batch not complete/currently verified")
    _require(row["per_code_expected_amount"] == str(sum(amounts)), "GUI froze wrong amount")
    request = json.loads((batch / "request.json").read_bytes())
    _require(tuple(int(item["amount"]) for item in request["sets"]) == tuple(amounts), "actual request differs from visible frozen input")
    _require(_hash(batch / "request.json") == row["request_sha256"] and _hash(batch / "manifest.json") == row["manifest_sha256"], "original native fingerprints changed")
    audit = window.catalog.audit_record(row["latest_audit_id"])
    _require(audit["status"] == "PASS" and len(audit["checks"]) == 15, "fifteen real checks missing")
    pairs, identities, checks = set(), set(), []
    for execution in audit["checks"]:
        method, number = execution["method"], execution["round"]
        pairs.add((method, number))
        path = batch / execution["report_relpath"]
        actual = json.loads(path.read_bytes())
        _require(actual["status"] == "PASS" and actual["errors"] == [] and execution["exit_code"] == 0, "worker failed")
        _require(_hash(path) == execution["report_sha256"], "worker report fingerprint differs")
        _require(actual["pid"] == execution["pid"] and actual["process_created_filetime"] == int(execution["process_created_filetime"]), "worker OS identity differs")
        _require(actual["method"] == method and actual["round"] == number and actual["audit_id"] == audit["audit_id"]
                 and actual["batch_id"] == bid and actual["execution_id"] == execution["execution_id"], "report session identity differs")
        identities.add((actual["pid"], actual["process_created_filetime"]))
        runtime = actual["runtime"]
        _require(runtime["frozen"] is True and Path(runtime["executable"]).resolve() == Path(sys.executable).resolve(), "worker was not candidate exe")
        _require(Path(runtime["checker_module"]).resolve(strict=False).is_relative_to(bundle), "checker source fallback")
        actual_stats = {"actual_raw_total": n, "actual_group_count": row["group_count"], "actual_total_amount": str(total),
                        "checked_codes": 1000, "checked_source_code_cells": n, "files_read": 5}
        _require(all(type(actual.get(key)) is type(value) and actual.get(key) == value for key, value in actual_stats.items()), "worker actual statistics wrong")
        _require(all(actual["proofs"].get(key) is True for key in (*COMMON_PROOFS, *EXTRA_PROOFS[method])), "worker required proof missing/false")
        checks.append({"method": method, "round": number, "pid": execution["pid"],
                       "process_created_filetime": execution["process_created_filetime"], "runtime": runtime,
                       "report_path": str(path), "report_sha256": execution["report_sha256"], "exit_code": execution["exit_code"]})
    _require(pairs == {(method, number) for method in "ABC" for number in range(1, 6)} and len(identities) == 15,
             "fifteen combinations/process instances incomplete")
    return {"audit_id": audit["audit_id"], "checks_passed": 15, "checks": checks}


def _result(app, window, bid, amounts):
    from PySide6.QtWidgets import QApplication
    batch = window.catalog.path(bid)
    _require(window.results_page.store.batch_id == bid, "GUI store belongs to another selected batch")
    rows = [json.loads(line) for line in (batch / "items.jsonl").read_bytes().splitlines()]
    blocks = (batch / "groups.txt").read_text(encoding="utf-8").split("\n\n")
    raw_counts, final_amounts = [0] * 1000, [0] * 1000
    ids = set()
    for row in rows:
        item_id = row["item_id"]
        _require(item_id not in ids and 0 <= item_id < len(amounts) * 1000, "actual source IDs incomplete/duplicate")
        ids.add(item_id)
        _require(row["set_no"] == item_id // 1000 + 1 and row["code"] == f"{item_id % 1000:03d}"
                 and row["amount"] == str(amounts[item_id // 1000]), "actual source tuple differs")
        raw_counts[int(row["code"])] += 1
    _require(len(ids) == len(amounts) * 1000, "actual full source pool missing")
    for block in blocks:
        for line in block.rstrip("\n").split("\n")[1:]:
            codes, amount = line.split("直选")
            for code in codes.split("."): final_amounts[int(code)] += int(amount[:-1])
    selected = []
    for group in dict.fromkeys((1, 2, len(blocks))):
        window.results_page.group_spin.setValue(group); app.processEvents()
        body = blocks[group - 1].split("\n", 1)[1].rstrip("\n") + "\n"
        _require(window.results_page.body_edit.toPlainText().rstrip("\n") == body.rstrip("\n"), "GUI body differs from actual selected TXT group")
        originals = [row for row in rows if row["group"] == group]
        _require(window.results_page.source_model.records == originals and 2 <= len(originals) <= 5, "GUI source table differs from selected original records")
        warning_index = len(_QT_MESSAGES)
        window.results_page.copy_button.click(); app.processEvents()
        copy_started = time.monotonic()
        # Let the packaged application's own bounded QTimer retries finish.
        # The QA harness never clicks again or manufactures the copied body.
        while ((QApplication.clipboard().text() != body or "已复制本组正文" not in window.results_page.summary_label.text())
               and time.monotonic() - copy_started < 2):
            app.processEvents(); time.sleep(0.01)
        clipboard = QApplication.clipboard().text()
        _require(clipboard == body, f"clipboard differs for group {group}: expected={body!r}; actual={clipboard!r}; enabled={window.results_page.copy_button.isEnabled()}; summary={window.results_page.summary_label.text()}")
        _require("已复制本组正文" in window.results_page.summary_label.text(), "product did not confirm clipboard read-back")
        selected.append({"group": group, "body": body, "raw_records": originals, "clipboard_exact": True,
                         "clipboard_observed_seconds": time.monotonic() - copy_started,
                         "qa_button_retry_count": 0, "product_confirmed_readback": True,
                         "native_messages_during_copy": _QT_MESSAGES[warning_index:]})
    window.results_page.group_spin.setValue(1)
    window.results_page.next_button.click(); _require(window.results_page.group_spin.value() == 2, "next group button failed")
    window.results_page.previous_button.click(); _require(window.results_page.group_spin.value() == 1, "previous group button failed")
    stats = window.stats_page.table_model.rows
    _require(len(stats) == 1000 and [item["code"] for item in stats] == [f"{n:03d}" for n in range(1000)], "statistics do not cover all codes")
    for number, item in enumerate(stats):
        _require(item["actual_raw"] == raw_counts[number] == len(amounts)
                 and item["actual_amount"] == str(final_amounts[number]) == str(sum(amounts))
                 and item["raw_difference"] == 0 and item["amount_difference"] == "0", "GUI actual per-code statistics differ")
    return {"selected_groups": selected, "statistics": stats, "source_record_count": len(rows), "fixed_files": _fixed(batch)}


def _open_history(app, window, bid):
    history = window.history_page
    window.tabs.setCurrentWidget(history)
    history.refresh()
    for row, record in enumerate(history.model.rows):
        if record["batch_id"] == bid:
            history.table.setCurrentIndex(history.model.index(row, 0))
            _button(history, "打开选中批次").click()
            break
    else:
        raise ValueError("requested real batch is not on current history page")
    _wait(app, lambda: not window.busy, "history result load", window, 30)


def _clipboard_retry_and_cancel(app, window, other_bid, root):
    """A brief genuine OS clipboard lock; no clipboard fakes or business patches."""
    import ctypes
    from ctypes import wintypes
    import threading
    from PySide6.QtWidgets import QApplication
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.OpenClipboard.argtypes = [wintypes.HWND]; user.OpenClipboard.restype = wintypes.BOOL
    user.CloseClipboard.argtypes = []; user.CloseClipboard.restype = wintypes.BOOL
    def hold(seconds=0.16):
        ready = threading.Event(); state = {}
        def worker():
            opened = False
            try:
                end = time.monotonic() + 1
                while time.monotonic() < end:
                    if user.OpenClipboard(None): opened = True; break
                    time.sleep(0.01)
                if not opened: raise ctypes.WinError(ctypes.get_last_error())
                state["opened"] = True; ready.set(); time.sleep(seconds)
            except BaseException as error:
                state["error"] = str(error); ready.set()
            finally:
                if opened: state["closed"] = bool(user.CloseClipboard())
        thread = threading.Thread(target=worker, name="QA-brief-OpenClipboard-contention")
        thread.start(); _require(ready.wait(2) and state.get("opened"), "QA could not establish real clipboard contention: " + str(state))
        return thread, state
    def pump(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end: app.processEvents(); time.sleep(0.01)
    def sentinel(value):
        for _ in range(3):
            QApplication.clipboard().setText(value); app.processEvents()
            if QApplication.clipboard().text() == value: return
            pump(0.1)
        raise ValueError("QA clipboard sentinel setup unavailable")
    page = window.results_page
    selected_bid = page.store.batch_id
    page.group_spin.setValue(1)
    body = page.store.get_group(1)["body"]
    warning_index = len(_QT_MESSAGES); before = time.monotonic()
    thread, state = hold()
    page.copy_button.click(); app.processEvents()
    _require("正在重试" in page.summary_label.text(), "actual native busy did not enter product retry path")
    _wait(app, lambda: QApplication.clipboard().text() == body and "已复制本组正文" in page.summary_label.text(),
          "single-click product clipboard retry", window, 2)
    thread.join(2)
    _require(state.get("closed") and any("0x800401d0" in item["message"].lower() for item in _QT_MESSAGES[warning_index:]),
             "real native clipboard-busy HRESULT not observed")
    retry = {"fault_kind": "brief genuine Win32 OpenClipboard contention on this desktop", "hold_seconds": 0.16,
             "actual_button_clicks": 1, "qa_button_retry_count": 0, "product_automatic_readback_confirmed": True,
             "elapsed_seconds": time.monotonic() - before, "native_messages": _QT_MESSAGES[warning_index:]}
    _write(root / "clipboard-retry-checkpoints.json", {"retry": retry})
    thread, state = hold(0.8); page.copy_button.click()
    pending_summary = page.summary_label.text()
    page.group_spin.setValue(2)  # No event pump before cancelling the old timer.
    thread.join(2)
    _require(state.get("closed") and "正在重试" in pending_summary, "QA did not establish a pending copy before group change: " + str(state) + pending_summary)
    # A failed native OleSetClipboard may itself clear the old clipboard. Install
    # the observation sentinel only after native contention ends, before any
    # queued retry can run; never assume that failed OS writes preserve content.
    QApplication.clipboard().setText("QA-pending-group-must-not-overwrite")
    _require(QApplication.clipboard().text() == "QA-pending-group-must-not-overwrite", "post-contention QA sentinel unavailable")
    pump(0.9)
    group_observed = {"closed": state.get("closed"), "pending_summary": pending_summary,
                      "actual_clipboard": QApplication.clipboard().text(), "old_body": body,
                      "group": page.group_spin.value(), "summary": page.summary_label.text()}
    _write(root / "clipboard-retry-checkpoints.json", {"retry": retry, "group_cancel": group_observed})
    _require(QApplication.clipboard().text() == "QA-pending-group-must-not-overwrite" and page.group_spin.value() == 2,
             "old pending group copied after group selection changed: " + str(group_observed))
    retry["pending_cancelled_on_group_change"] = True
    page.group_spin.setValue(1)
    thread, state = hold(0.8); page.copy_button.click()
    pending_summary = page.summary_label.text(); window.open_batch(other_bid)
    _require(window.busy and page.store is None and not page.copy_button.isEnabled(), "store switch did not clear copy while pending")
    thread.join(2)
    _require(state.get("closed") and "正在重试" in pending_summary, "QA did not establish pending copy before store change")
    QApplication.clipboard().setText("QA-pending-store-must-not-overwrite")
    _require(QApplication.clipboard().text() == "QA-pending-store-must-not-overwrite", "post-contention store sentinel unavailable")
    _wait(app, lambda: not window.busy, "new store load with stale pending copy", window, 30); pump(0.9)
    store_observed = {"closed": state.get("closed"), "pending_summary": pending_summary,
                      "actual_clipboard": QApplication.clipboard().text(), "store_bid": page.store.batch_id}
    _write(root / "clipboard-retry-checkpoints.json", {"retry": retry, "group_cancel": group_observed, "store_cancel": store_observed})
    _require(page.store.batch_id == other_bid and QApplication.clipboard().text() == "QA-pending-store-must-not-overwrite",
             "old pending store copied after batch selection changed: " + str(store_observed))
    retry["pending_cancelled_on_store_change"] = True
    window.open_batch(selected_bid); _wait(app, lambda: not window.busy, "restore selected store after clipboard QA", window, 30)
    return retry


def _generate(app, window, amounts, root, label, bundle):
    expected = (1, 5, 9) if len(amounts) == 3 else tuple(amounts)
    # Resume only an already completed matching QA batch; never randomize it again.
    matching = [row for row in window.catalog.list_page(limit=200)
                if row["generation_status"] == "COMPLETED"
                and tuple(int(item["amount"]) for item in json.loads(window.catalog.get(row["batch_id"])["original_request_json"])["sets"]) == expected]
    if matching:
        bid = matching[0]["batch_id"]
        window.open_batch(bid); _wait(app, lambda: not window.busy, "reused completed result load", window, 30)
        generated = False
        visible = {"reused_previous_completed_QA_batch": True}
    else:
        window.tabs.setCurrentWidget(window.new_page)
        new = window.new_page; new.count_input.setValue(len(amounts))
        if len(amounts) == 3:
            for row, amount in enumerate(amounts): _editor(app, new, row, amount, commit=True)
            _require(new.model.amount_strings() == ("1", "5", "2"), "three-set initial model differs")
            editor = _editor(app, new, 2, "9")
            _require(new.model.amount_strings() == ("1", "5", "2") and editor.text() == "9", "last editor was already committed before Start")
            _require("15元" in new.summary_label.text(), "visible last amount not in realtime preview")
        else:
            editor = _editor(app, new, 0, "1")
        visible = {"model": list(new.model.amount_strings()), "editor": editor.text(), "summary": new.summary_label.text()}
        previous = {row["batch_id"] for row in window.catalog.list_page(limit=200)}
        _require(new.start_button.isEnabled(), "positive visible input did not enable Start")
        new.start_button.click()
        _require(new.frozen_spec.amounts == expected, "Start did not commit visible editor before freezing")
        _write(root / "progress.json", {"phase": label + "_real_QThread_generation", "pid": os.getpid()})
        _wait(app, lambda: not window.busy and window.results_page.store is not None
              and window.results_page.store.batch_id not in previous and window.stats_page.table_model.rowCount() == 1000,
              "GUI generation/automatic result load", window)
        bid = window.results_page.store.batch_id
        generated = True
        _require(all("PASS" in item.text() for item in window.check_labels.values()), "actual GUI fifteen labels not PASS")
    audit = _audit(window, bid, expected, bundle)
    result = {"label": label, "batch_id": bid, "batch_dir": str(window.catalog.path(bid)),
              "actual_frozen_amounts": list(map(str, expected)), "generated_in_this_GUI_process": generated,
              "visible_input_before_start": visible, "generation_audit": audit}
    # Persist before subsequent GUI assertions so a harness interruption can reuse
    # the actual completed batch and its original fifteen reports.
    _write(root / (label + "-generation-receipt.json"), result)
    result["result_view"] = _result(app, window, bid, expected)
    backups = [row for row in window.catalog.backup_coverage(bid) if row["status"] == "COMPLETE"]
    _require(backups and all((Path(item["package_path"]) / "BACKUP_COMPLETE.json").is_file() for item in backups), "actual GUI self-contained backup missing")
    result["backups"] = backups
    window.grab().save(str(root / (label + "-results.png")))
    return result


def _soak(app, window, root, data, report):
    """One visible bundled window, real event loop, bounded QA timer/close control."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from number_app.amount_mode.ui.group_index import cache_info
    started = time.monotonic()
    state = {"last_health": -100.0, "last_action": -100.0, "actions": 0, "close_requested": False, "errors": []}
    def tick():
        now = time.monotonic()
        try:
            if (root / "REQUEST_NORMAL_CLOSE").exists():
                state["close_requested"] = True
                window.close()
                if not window.isVisible() and not window.busy:
                    _write(root / "NORMAL_CLOSE_ACK.json", {"pid": os.getpid(), "normal_close": True, "uptime_seconds": now - started})
                    app.quit()
                return
            pause = root / "PAUSE"
            if pause.exists():
                if not window.busy:
                    raw = pause.read_bytes()
                    _write(root / "PAUSED.json", {"pid": os.getpid(), "paused": True, "busy": False,
                                                  "request_sha256": hashlib.sha256(raw).hexdigest(), "request_mtime_ns": pause.stat().st_mtime_ns})
            else:
                if (root / "PAUSED.json").exists(): (root / "PAUSED.json").unlink()
                if not window.busy and now - state["last_action"] >= 10:
                    state["last_action"] = now
                    history = window.history_page
                    history.refresh()
                    if window.catalog.count() > history.page_size:
                        _button(history, "下一页" if state["actions"] % 2 == 0 else "上一页").click()
                    available = [row for row in history.model.rows if row["generation_status"] == "COMPLETED" and row["latest_verification_status"] == "PASS"]
                    if available:
                        if state["actions"] % 2 == 0 or window.results_page.store is None:
                            window.open_batch(available[0]["batch_id"])
                        else:
                            page = window.results_page
                            page.group_spin.setValue(min(page.store.group_count, 1 + state["actions"] % page.store.group_count))
                            page.copy_button.click()
                            window.tabs.setCurrentWidget(window.stats_page if state["actions"] % 3 else window.history_page)
                    state["actions"] += 1
            if now - state["last_health"] >= 10:
                state["last_health"] = now
                _write(root / "qa-gui-health.json", {"pid": os.getpid(), "frozen": True, "executable": sys.executable,
                    "uptime_seconds": now - started, "visible": window.isVisible(), "busy": window.busy,
                    "heartbeat_count": window._heartbeat_count, "history_count": window.catalog.count(),
                    "history_page": window.history_page.page, "periodic_actual_Qt_actions": state["actions"],
                    "paused": pause.exists(), "cache": cache_info(), "errors": state["errors"],
                    "data_root": str(data), "module_origins": report["runtime"]["module_origins"]})
        except BaseException as error:
            state["errors"].append(type(error).__name__ + ": " + str(error))
            _write(root / "qa-gui-errors.json", {"errors": state["errors"], "pid": os.getpid()})
    timer = QTimer(window); timer.setInterval(200); timer.timeout.connect(tick); timer.start()
    _write(root / "READY.json", {"pid": os.getpid(), "data_root": str(data), "started_monotonic": started,
                                 "pause_request": str(root / "PAUSE"), "pause_ack": str(root / "PAUSED.json"),
                                 "normal_close_request": str(root / "REQUEST_NORMAL_CLOSE")})
    tick(); exit_code = app.exec()
    timer.stop()
    report.update(status="PASS" if exit_code == 0 and state["close_requested"] and not state["errors"] else "FAIL",
                  uptime_seconds=time.monotonic() - started, periodic_actual_Qt_actions=state["actions"],
                  normal_close_requested=state["close_requested"], qt_exit_code=exit_code,
                  minimum_7200_seconds_observed=time.monotonic() - started >= 7200,
                  soak_errors=state["errors"])
    report["errors"].extend(state["errors"])


def run_frozen_gui(run_directory, scenario="one", existing_data=None):
    """Operate bundled Qt controls in this new child, never another process/window."""
    root = Path(run_directory)
    data = Path(existing_data) if existing_data else root / "data"
    report = {"status": "FAIL", "scenario": scenario, "pid": os.getpid(), "data_root": str(data), "errors": [], "batches": [],
              "interaction_kind": "internal Qt control/event test; not physical desktop mouse input"}
    window = app = None
    try:
        bundle, origins, dll_handles = _bundled_imports()
        from PySide6.QtCore import QCoreApplication, QEvent, qInstallMessageHandler
        from PySide6.QtWidgets import QApplication
        from number_app.amount_mode.ui.app import AmountWindow
        from number_app.amount_mode.ui.group_index import clear_memory_cache
        from number_app.storage.locking import WorkspaceLock
        app = QApplication.instance() or QApplication(["AmountTool-QA-internal-GUI"])
        app.setQuitOnLastWindowClosed(False)
        _QT_MESSAGES.clear(); _CLIPBOARD_RETRIES.clear()
        report["qt_messages"] = _QT_MESSAGES
        report["native_clipboard_retries"] = _CLIPBOARD_RETRIES
        def qt_message(kind, context, message):
            if len(report["qt_messages"]) < 100:
                report["qt_messages"].append({"type": str(kind), "message": message})
        qInstallMessageHandler(qt_message)
        report["runtime"] = {"executable": sys.executable, "frozen": True, "bundle_root": str(bundle),
                             "module_origins": origins, "qa_target": __file__}
        _write(root / "progress.json", {"phase": "bundled_gui_imported", "pid": os.getpid()})
        window = AmountWindow(data)
        window.setWindowTitle("按套金额随机小组 1.1 — QA内部Qt验收")
        window.show(); app.processEvents()
        _require(window.catalog is not None, "bundled AmountWindow could not initialize QA catalog: " + window.stage_label.text())
        if scenario == "soak":
            _soak(app, window, root, data, report)
            _write(root / "gui-result.json", report)
            if report["status"] != "PASS": raise SystemExit(3)
            return
        report["batches"].append(_generate(app, window, (1,), root, "one", bundle))
        if scenario == "full":
            report["batches"].append(_generate(app, window, (1, 5, 2), root, "three-visible-nine", bundle))
            report["product_clipboard_retry_and_pending_cancellation"] = _clipboard_retry_and_cancel(app, window, report["batches"][0]["batch_id"], root)
            three = report["batches"][-1]; bid = three["batch_id"]
            # Poison only a derived cache, retaining its claimed correct hashes.
            store = window.results_page.store
            cache = store.cache_file
            payload = json.loads(cache.read_bytes())
            original_groups_hash = payload["groups_sha256"]
            for key in ("text_offsets", "item_offsets"): payload[key][0:2] = payload[key][1:3]
            _write(cache, payload)
            clear_memory_cache()
            window.open_batch(bid)
            _wait(app, lambda: not window.busy, "wrong-offset actual GUI reload", window, 30)
            actual = _result(app, window, bid, (1, 5, 9))
            repaired = json.loads(cache.read_bytes())
            _require(repaired["text_offsets"][0] == repaired["item_offsets"][0] == 0
                     and repaired["groups_sha256"] == original_groups_hash, "wrong-offset cache was not rebuilt for selected group")
            report["wrong_offset_cache"] = {"claimed_hash_unchanged": True, "repaired_first_offsets": [repaired["text_offsets"][0], repaired["item_offsets"][0]],
                                            "actual_group_one": actual["selected_groups"][0], "fixed_files_unchanged": actual["fixed_files"] == three["result_view"]["fixed_files"]}
            # Explicit QA status fault, not a claim that a failed 15-check session
            # was run. The actual bundled result/copy guard must pause immediately.
            with WorkspaceLock(data): window.catalog.mark_verification(bid, "FAIL", error="QA-only latest-verification-state injection")
            QApplication.clipboard().setText("QA-paused-result-must-not-copy")
            window.results_page.copy_button.click(); app.processEvents()
            _require(not window.results_page.copy_button.isEnabled() and not window.results_page.body_edit.toPlainText()
                     and QApplication.clipboard().text() == "QA-paused-result-must-not-copy", "recent verification failure did not pause actual copy/view")
            report["latest_failure_pause"] = {"fault_kind": "explicit QA state injection using bundled catalog API", "status": "FAIL", "copy_disabled": True, "body_cleared": True}
            window.grab().save(str(root / "latest-failure-paused.png"))
            # Cross-batch selection is a separate invariant: selecting failed B
            # must not leave an earlier valid A's store or copy action available.
            other = report["batches"][0]["batch_id"]
            window.open_batch(other)
            _wait(app, lambda: not window.busy, "other valid result before failed selection", window, 30)
            _require(window.results_page.store.batch_id == other, "valid A fixture did not load")
            QApplication.clipboard().setText("QA-cross-selection-must-not-copy-old-A")
            window.open_batch(bid); app.processEvents()
            _require(window._selected_bid == bid and window.results_page.store is None
                     and not window.results_page.copy_button.isEnabled() and not window.results_page.body_edit.toPlainText()
                     and window.stats_page.table_model.rowCount() == 0, "failed B selection retained another batch's copy/view/statistics")
            window.results_page.copy_button.click(); app.processEvents()
            _require(QApplication.clipboard().text() == "QA-cross-selection-must-not-copy-old-A", "failed B selection copied previous A")
            window.open_batch(other)
            _require(window.busy and window.results_page.store is None and not window.results_page.copy_button.isEnabled(),
                     "new result load left a stale batch copy available")
            _wait(app, lambda: not window.busy, "valid result after selection guard", window, 30)
            report["selection_binding"] = {"failed_selection_clears_other_store": True, "copy_disabled_during_load": True,
                                            "statistics_cleared_for_failed_selection": True}
            window.close(); app.processEvents(); window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete); app.processEvents()
            window = AmountWindow(data); window.show(); app.processEvents()
            _require(window.history_page.model.rowCount() == 2, "real GUI history after close/reopen lost batches")
            history = window.history_page; history.page_size = 1; history.refresh()
            first_page = [item["batch_id"] for item in history.model.rows]
            _button(history, "下一页").click(); second_page = [item["batch_id"] for item in history.model.rows]
            _button(history, "上一页").click()
            _require(len(first_page) == len(second_page) == 1 and set(first_page + second_page) == {item["batch_id"] for item in report["batches"]}, "actual indexed history page navigation differs")
            report["history_pagination"] = {"real_completed_batches": 2, "page_size_for_probe": 1, "first_page": first_page, "second_page": second_page}
            window.grab().save(str(root / "history-pagination.png"))
            history.page_size = 30; history.page = 0; history.refresh()
            for item in report["batches"]:
                bid = item["batch_id"]; amounts = tuple(map(int, item["actual_frozen_amounts"]))
                _open_history(app, window, bid)
                window.tabs.setCurrentWidget(window.tools_page)
                old_audit = item["generation_audit"]["audit_id"]
                _button(window.tools_page, "独立复核所选批次").click()
                _wait(app, lambda: window._current_bid == bid and window._audit_id is not None and window.busy,
                      "real reverify progress started", window, 30)
                stage, pending, selection = window.stage_label.text(), window._pending_done, window._selected_bid
                window._on_progress({"token": "stale-task", "event": {"batch_id": bid, "phase": "check_finished", "method": "A", "round": 1, "status": "FAIL"}})
                window._on_done({"token": "stale-task", "ok": True, "value": {"status": "COMPLETED"}})
                window._on_progress({"token": window._token, "event": {"batch_id": "other-batch", "phase": "COMPLETED"}})
                window._on_progress({"token": window._token, "event": {"batch_id": bid, "audit_id": "other-audit", "phase": "COMPLETED"}})
                _require(window.stage_label.text() == stage and window._pending_done is pending and window._selected_bid == selection,
                         "stale token/batch/audit event changed current GUI task")
                _wait(app, lambda: not window.busy, "real history reverify fifteen", window)
                item["history_reverify"] = _audit(window, bid, amounts, bundle)
                _require(item["history_reverify"]["audit_id"] != old_audit, "history reverify reused old session")
                _open_history(app, window, bid)
                final_view = _result(app, window, bid, amounts)
                _require(final_view["fixed_files"] == item["result_view"]["fixed_files"], "history/reverify regenerated or changed fixed results")
                item["fixed_files_unchanged_after_history_reverify"] = True
            report["stale_events_ignored"] = True
        report["checks_passed"] = sum(item["generation_audit"]["checks_passed"] + item.get("history_reverify", {}).get("checks_passed", 0)
                                        for item in report["batches"])
        report["heartbeat_count"] = window._heartbeat_count
        window.close(); app.processEvents()
        _require(not window.isVisible() and not window.busy, "GUI did not close cleanly")
        report["status"] = "PASS"
        _write(root / "progress.json", {"phase": "completed", "pid": os.getpid(), "checks_passed": report["checks_passed"]})
    except BaseException as error:
        report["errors"].append(type(error).__name__ + ": " + str(error))
        report["traceback"] = traceback.format_exc()
        if window is not None and not window.busy:
            window.close()
            if app is not None: app.processEvents()
    _write(root / "gui-result.json", report)
    if report["status"] != "PASS": raise SystemExit(3)
