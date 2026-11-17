import copy
import json
import multiprocessing
import os
import shutil
import time
import uuid
from pathlib import Path

import pytest

from number_app.amount_mode import audit as audit_module
from number_app.amount_mode.audit import audit_amount_batch
from number_app.amount_mode.protocol import canonical_json, digest, strict_json
from number_app.amount_mode.reports import validate_audit_session, validate_report, validate_summary
from .helpers import candidate

RUN = Path(os.environ.get("AMOUNT_QA_ROOT", "E:/number/qa/amount_mode/r17-ddb2bc"))


def _sleep_worker(*args):
    time.sleep(30)


def _crash_worker(*args):
    os._exit(7)


def _watchdog_stall_worker(batch_dir, context, method, round_no, execution_id, report_path):
    import threading
    from number_app.amount_mode.audit import _watch_parent, process_creation_filetime
    threading.Thread(target=_watch_parent, daemon=True).start()
    Path(report_path).with_suffix(".started.json").write_bytes(canonical_json({
        "pid": os.getpid(), "process_created_filetime": process_creation_filetime()}))
    time.sleep(30)


def _abandon_parent(batch_dir, context, evidence_path):
    from number_app.amount_mode import audit as module
    module._audit_worker = _watchdog_stall_worker
    def progress(event):
        if event["phase"] == "check_started":
            marker = Path(batch_dir) / "audits" / event["audit_id"] / (event["report_path"].removesuffix(".json") + ".started.json")
            limit = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < limit:
                time.sleep(0.02)
            Path(evidence_path).write_bytes(canonical_json({"parent_event": event,
                "child_started": strict_json(marker.read_bytes())}))
            os._exit(91)
    module.audit_amount_batch(batch_dir, context, progress=progress)


def _job_only_stall_worker(batch_dir, context, method, round_no, execution_id, report_path):
    # Deliberately no Python parent watchdog: the OS job must own cleanup even
    # before a real checker has completed interpreter/bootstrap imports.
    Path(report_path).with_suffix(".started.json").write_bytes(canonical_json({"pid": os.getpid()}))
    time.sleep(60)


def _job_parent_waiting_for_hard_kill(batch_dir, context, evidence_path):
    from number_app.amount_mode import audit as module
    module._audit_worker = _job_only_stall_worker
    assigned = {}
    original = module.WindowsKillJob.assign
    def assign(job, handle):
        original(job, handle)
        assigned.update(in_owned_job=job.contains(handle), job_inheritable=job.inheritable)
    module.WindowsKillJob.assign = assign
    def progress(event):
        if event["phase"] == "check_started":
            Path(evidence_path).write_bytes(canonical_json({"event": event, **assigned,
                "watchdog_installed_by_qa_child": False}))
            time.sleep(60)
    module.audit_amount_batch(batch_dir, context, progress=progress)


@pytest.fixture(scope="module")
def genuine_audit():
    root = RUN / "audit-native" / uuid.uuid4().hex[:10]
    context = candidate(root, (1, 5, 2))
    events = []
    outcome = audit_amount_batch(root, context, progress=events.append)
    assert outcome.passed, outcome.errors
    assert len(outcome.checks) == 15 and outcome.checks_passed == 15
    return root, outcome, events


def test_real_fifteen_fresh_processes_and_strict_summary(genuine_audit):
    root, result, events = genuine_audit
    assert [event["phase"] for event in events].count("check_started") == 15
    assert [event["phase"] for event in events].count("check_finished") == 15
    assert [(r["method"], r["round"]) for r in result.checks] == [(m, r) for r in range(1, 6) for m in "ABC"]
    assert len({r["execution_id"] for r in result.checks}) == 15
    assert len({(r["pid"], r["process_created_filetime"]) for r in result.checks}) == 15
    assert all(r["exit_code"] == 0 for r in result.checks)
    assert all(event["audit_id"] == result.context.audit_id for event in events)
    assert all(event["batch_id"] == result.context.batch_id for event in events)
    assert validate_audit_session(root, result.context, result.checks, result.session_dir) == (True, [])
    assert validate_summary(result.session_dir, result.context, result.checks, result.summary_sha256) == (True, [])
    assert not multiprocessing.active_children()


def test_fixture_all_eleven_gate_faults_reject_actual_report_copies(genuine_audit, tmp_path):
    source, original, _ = genuine_audit
    fixture = json.loads((Path(__file__).parents[2] / "按套金额随机小组_验收样例.json").read_text(encoding="utf-8"))
    evidence = []
    for fault in fixture["audit_gate_cases"]:
        root = tmp_path / fault
        shutil.copytree(source, root)
        checks = copy.deepcopy(list(original.checks))
        session = root / "audits" / original.context.audit_id
        report_file = session / checks[0]["report_path"]
        report = strict_json(report_file.read_bytes())
        if fault == "missing_report":
            report_file.unlink()
        elif fault == "duplicate_method_round":
            checks[1] = copy.deepcopy(checks[0])
        elif fault == "mixed_audit_id":
            report["audit_id"] = uuid.uuid4().hex
        elif fault == "mixed_snapshot_digest":
            report["expected_manifest_sha256"] = "0" * 64
        elif fault == "mixed_request_fingerprint":
            report["expected_request_sha256"] = "0" * 64
        elif fault == "pass_with_missing_proof":
            report["proofs"].pop("source_code_cells_exact")
        elif fault == "pass_with_false_proof":
            report["proofs"]["source_code_cells_exact"] = False
        elif fault == "pass_with_null_actual_stat":
            report["actual_raw_total"] = None
        elif fault == "nonzero_exit_with_pass":
            checks[0]["exit_code"] = 3
        elif fault == "data_changed_after_reports":
            p = root / "groups.txt"
            p.write_bytes(p.read_bytes() + b"\n")
        elif fault == "report_changed_before_summary":
            report_file.write_bytes(report_file.read_bytes() + b"\n")
        else:
            pytest.fail("未实现固定门槛故障：" + fault)
        if fault in {"mixed_audit_id", "mixed_snapshot_digest", "mixed_request_fingerprint",
                     "pass_with_missing_proof", "pass_with_false_proof", "pass_with_null_actual_stat"}:
            data = canonical_json(report)
            report_file.write_bytes(data)
            # Candidate receipt hash is self-consistent, so semantic report
            # checks, rather than only a stale hash, must reject these cases.
            checks[0]["report_sha256"] = digest(data)
        valid, errors = validate_audit_session(root, original.context, checks, session)
        assert valid is False and errors, fault
        evidence.append({"case_id": fault, "status": "REJECTED", "errors": errors, "batch_dir": str(root)})
    (tmp_path / "gate_fault_results.json").write_bytes(canonical_json(evidence))


def test_report_strict_types_summary_tamper_and_malformed_metadata(genuine_audit, tmp_path):
    source, original, _ = genuine_audit
    record = original.checks[0]
    report = strict_json((original.session_dir / record["report_path"]).read_bytes())
    for key in ("actual_raw_total", "actual_group_count", "checked_codes", "checked_source_code_cells", "files_read"):
        broken = copy.deepcopy(report)
        broken[key] = True
        assert validate_report(broken, original.context, record), key
    broken = copy.deepcopy(report)
    broken["proofs"]["source_code_cells_exact"] = 1
    assert validate_report(broken, original.context, record)
    for checks in (None, {}, [], [None] * 15):
        assert validate_audit_session(source, original.context, checks, original.session_dir)[0] is False
    for record in (None, {"method": [], "round": 1}):
        assert validate_report(report, original.context, record)
    root = tmp_path / "copy"
    shutil.copytree(source, root)
    session = root / "audits" / original.context.audit_id
    summary_file = session / "summary.json"
    summary = strict_json(summary_file.read_bytes())
    summary["checks_passed"] = True
    data = canonical_json(summary)
    summary_file.write_bytes(data)
    assert validate_summary(session, original.context, original.checks, digest(data))[0] is False


def test_real_abnormal_exit_timeout_cancel_and_process_cleanup(tmp_path, monkeypatch):
    root = tmp_path / "batch"
    context = candidate(root, (1,))
    monkeypatch.setattr(audit_module, "_audit_worker", _crash_worker)
    crashed = audit_amount_batch(root, context)
    assert crashed.status == "FAIL" and crashed.checks_passed == 0
    assert crashed.checks[0]["exit_code"] == 7 and crashed.errors
    assert not multiprocessing.active_children()
    monkeypatch.setattr(audit_module, "_audit_worker", _sleep_worker)
    timed_out = audit_amount_batch(root, context, timeout=0.05)
    assert timed_out.status == "TIMEOUT" and timed_out.checks_passed == 0 and timed_out.errors
    assert not multiprocessing.active_children()
    events = []
    cancelled = audit_amount_batch(root, context, cancel=lambda: bool(events), progress=events.append)
    assert cancelled.status == "CANCELLED" and cancelled.checks_passed == 0
    assert not multiprocessing.active_children()
    # Cancel while a real child is running, not only before launch.
    state = {"started": False}
    def event(value):
        if value["phase"] == "check_started":
            state["started"] = True
    cancelled_live = audit_amount_batch(root, context, cancel=lambda: state["started"], progress=event)
    assert cancelled_live.status == "CANCELLED" and len(cancelled_live.checks) == 1
    assert not multiprocessing.active_children()
    for value in (True, float("inf"), float("nan"), -1):
        with pytest.raises(ValueError):
            audit_amount_batch(root, context, timeout=value)


def test_session_uuid_collision_preserves_previous_summary(genuine_audit, monkeypatch):
    root, original, _ = genuine_audit
    before = {p.name: p.read_bytes() for p in original.session_dir.iterdir()}
    class Existing:
        hex = original.context.audit_id
    monkeypatch.setattr(audit_module.uuid, "uuid4", lambda: Existing())
    with pytest.raises(FileExistsError):
        audit_amount_batch(root, original.context)
    assert before == {p.name: p.read_bytes() for p in original.session_dir.iterdir()}


def test_real_parent_hard_exit_watchdog_stops_orphan(tmp_path):
    import ctypes
    from ctypes import wintypes
    from number_app.amount_mode.audit import process_creation_filetime
    root = tmp_path / "batch"
    context = candidate(root, (1,))
    evidence = tmp_path / "parent_exit.json"
    parent = multiprocessing.get_context("spawn").Process(target=_abandon_parent,
        args=(str(root), context, str(evidence)))
    parent.start()
    parent.join(15)
    try:
        assert not parent.is_alive()
        assert parent.exitcode == 91
    finally:
        if parent.is_alive():
            parent.kill()
            parent.join(10)
        parent.close()
    data = strict_json(evidence.read_bytes())
    child = data["child_started"]
    assert child["pid"] == data["parent_event"]["pid"]
    assert child["process_created_filetime"] == data["parent_event"]["process_created_filetime"]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000 | 0x1000, False, child["pid"])
    if handle:
        try:
            if process_creation_filetime(handle) == child["process_created_filetime"]:
                assert kernel.WaitForSingleObject(handle, 5000) == 0, "父进程已硬退，核查子进程仍残留"
        finally:
            kernel.CloseHandle(handle)
    data["observed_parent_exit_code"] = 91
    data["observed_child_terminated"] = True
    evidence.write_bytes(canonical_json(data))


def test_summary_write_failure_never_leaves_machine_pass(tmp_path, monkeypatch):
    root = tmp_path / "batch"
    context = candidate(root, (1,))
    actual_write = audit_module.synced_write
    def failing_write(path, data, **kwargs):
        if Path(path).name == "summary.txt":
            raise OSError("QA injected disk full during summary")
        return actual_write(path, data, **kwargs)
    monkeypatch.setattr(audit_module, "synced_write", failing_write)
    result = audit_amount_batch(root, context)
    assert result.status == "ERROR" and result.checks_passed == 15 and result.summary_sha256 is None
    assert not (result.session_dir / "summary.json").exists()
    assert not multiprocessing.active_children()


def test_os_job_reaps_child_without_watchdog_after_repeated_actual_parent_kills(tmp_path):
    import ctypes
    from ctypes import wintypes
    from number_app.amount_mode.audit import process_creation_filetime
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    outcomes = []
    for iteration in range(3):
        root = tmp_path / str(iteration) / "batch"
        context = candidate(root, (1,))
        evidence = root.parent / "job-before-parent-kill.json"
        parent = multiprocessing.get_context("spawn").Process(target=_job_parent_waiting_for_hard_kill,
            args=(str(root), context, str(evidence)))
        child_handle = None
        parent.start()
        try:
            deadline = time.monotonic() + 15
            while not evidence.exists() and parent.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert evidence.exists() and parent.is_alive()
            data = strict_json(evidence.read_bytes())
            assert data["in_owned_job"] is True and data["job_inheritable"] is False
            assert data["watchdog_installed_by_qa_child"] is False
            event = data["event"]
            child_handle = kernel.OpenProcess(0x100000 | 0x1000, False, event["pid"])
            assert child_handle
            assert process_creation_filetime(child_handle) == event["process_created_filetime"]
            assert kernel.WaitForSingleObject(child_handle, 0) == 258  # WAIT_TIMEOUT: child alive
            before = time.monotonic()
            parent.kill()  # actual TerminateProcess, no Python cleanup/finally
            parent.join(5)
            assert not parent.is_alive()
            waited = kernel.WaitForSingleObject(child_handle, 5000)
            assert waited == 0, "parent gone; exact child process still active despite job"
            data.update(parent_killed_exit_code=parent.exitcode, child_wait_result=waited,
                        elapsed_after_parent_kill=time.monotonic() - before)
            outcomes.append(data)
            evidence.write_bytes(canonical_json(data))
        finally:
            if parent.is_alive():
                parent.kill(); parent.join(5)
            parent.close()
            if child_handle:
                kernel.CloseHandle(child_handle)
    (tmp_path / "job-parent-kill-results.json").write_bytes(canonical_json(outcomes))


def test_job_assignment_failure_is_error_before_check_started_and_cleans_child(tmp_path, monkeypatch):
    root = tmp_path / "batch"
    context = candidate(root, (1,))
    def denied(self, process_handle):
        raise OSError("QA injected AssignProcessToJobObject denial")
    monkeypatch.setattr(audit_module.WindowsKillJob, "assign", denied)
    monkeypatch.setattr(audit_module, "_audit_worker", _sleep_worker)
    events = []
    outcome = audit_amount_batch(root, context, progress=events.append)
    assert outcome.status == "ERROR" and outcome.checks_passed == 0
    assert all(event["phase"] != "check_started" for event in events)
    assert any("AssignProcessToJobObject" in error for error in outcome.errors)
    assert not multiprocessing.active_children()
