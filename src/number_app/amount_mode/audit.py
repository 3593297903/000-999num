"""Fifteen fresh spawn processes, independent disk reads and strict parent gate."""
import ctypes
import math
import multiprocessing
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from multiprocessing.connection import wait
from pathlib import Path

from .limits import CHECK_TIMEOUT_SECONDS
from .protocol import CHECKER_VERSION, canonical_json, digest, read_snapshot, synced_write
from .reports import (REPORT_VERSION, expected_statistics, read_report_bytes, parse_report_bytes,
                      validate_audit_session, validate_report)
from .windows_job import WindowsKillJob


def _now():
    return datetime.now().astimezone().isoformat()


def process_creation_filetime(handle=None):
    """Exact OS creation identity, avoiding lossy epoch/ISO float round trips."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
    process_handle = kernel.GetCurrentProcess() if handle is None else wintypes.HANDLE(handle)
    if not kernel.GetProcessTimes(process_handle, ctypes.byref(created), ctypes.byref(exited),
                                  ctypes.byref(kernel_time), ctypes.byref(user_time)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (created.dwHighDateTime << 32) | created.dwLowDateTime


def _watch_parent():
    parent = multiprocessing.parent_process()
    if parent is not None:
        wait([parent.sentinel])
        os._exit(6)


def _audit_worker(batch_dir, context, method, round_no, execution_id, report_path):
    threading.Thread(target=_watch_parent, daemon=True, name="amount-parent-watchdog").start()
    started, before = _now(), time.monotonic()
    report = {
        "report_version": REPORT_VERSION, "mode": context.mode, "batch_id": context.batch_id,
        "audit_id": context.audit_id, "checker_version": CHECKER_VERSION,
        "execution_id": execution_id, "method": method, "round": round_no,
        "provenance": context.provenance, "expected_request_sha256": context.expected_request_sha256,
        "expected_manifest_sha256": context.expected_manifest_sha256,
        "pid": os.getpid(), "process_created_filetime": process_creation_filetime(),
        "started_at": started, "expected": expected_statistics(context),
        "proofs": {}, "actual_raw_total": None, "actual_group_count": None,
        "actual_total_amount": None, "checked_codes": None, "checked_source_code_cells": None,
        "files_read": None, "errors": [], "status": "ERROR",
        "runtime": {"executable": sys.executable, "frozen": bool(getattr(sys, 'frozen', False)),
                    "bundle_root": getattr(sys, '_MEIPASS', None), "checker_module": None},
    }
    exit_code = 4
    try:
        if method == "A":
            from .checks.a_count import check_a
            checker = check_a
        elif method == "B":
            from .checks.b_sort import check_b
            checker = check_b
        elif method == "C":
            from .checks.c_ids import check_c
            checker = check_c
        else:
            raise ValueError("未知核查方法")
        report['runtime']['checker_module'] = sys.modules[checker.__module__].__file__
        actual = checker(batch_dir, context)
        # The worker does not manufacture any actual proof/statistic fields.
        for key in ("proofs", "actual_raw_total", "actual_group_count", "actual_total_amount",
                    "checked_codes", "checked_source_code_cells", "files_read"):
            report[key] = actual.get(key)
        report["status"] = "PASS"
        exit_code = 0
    except BaseException as error:
        report["status"] = "FAIL" if isinstance(error, ValueError) else "ERROR"
        report["errors"] = [f"{type(error).__name__}: {str(error)[:2000]}"]
        exit_code = 3 if isinstance(error, ValueError) else 4
    report["ended_at"] = _now()
    report["elapsed_seconds"] = time.monotonic() - before
    try:
        synced_write(report_path, canonical_json(report))
    except BaseException:
        exit_code = 4
    raise SystemExit(exit_code)


@dataclass(frozen=True)
class AuditOutcome:
    context: object
    status: str
    checks: tuple
    session_dir: Path
    summary_sha256: str | None
    errors: tuple[str, ...]
    checks_passed: int

    @property
    def passed(self):
        return self.status == "PASS"


def _notify(progress, context, phase, **fields):
    if progress is not None:
        progress({"phase": phase, "batch_id": context.batch_id, "audit_id": context.audit_id, **fields})


def audit_amount_batch(batch_dir, context, *, cancel=None, progress=None, timeout=CHECK_TIMEOUT_SECONDS):
    """Caller owns the workspace lock; child processes never acquire it."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("核查超时必须为正数")
    current = context.with_audit(uuid.uuid4().hex)
    read_snapshot(batch_dir, current)
    root = Path(batch_dir).resolve(strict=True)
    audits = root / "audits"
    audits.mkdir(exist_ok=True)
    if audits.is_symlink() or audits.is_junction() or audits.resolve() != root / "audits":
        raise ValueError("审查根目录不安全")
    session = audits / current.audit_id
    # Do not own a directory until exclusive creation succeeds. A UUID collision
    # must never overwrite a previous audit's summary in an exception handler.
    session.mkdir()
    checks, errors, passed, status = [], [], 0, "ERROR"
    started_at, before = _now(), time.monotonic()
    spawn = multiprocessing.get_context("spawn")
    job = None
    try:
        job = WindowsKillJob()
        _notify(progress, current, "audit_started")
        for round_no in range(1, 6):
            for method in "ABC":
                if cancel is not None and cancel():
                    status = "CANCELLED"
                    errors.append("审查已取消")
                    break
                execution_id = uuid.uuid4().hex
                name = f"{method}_{round_no}.json"
                execution = {"method": method, "round": round_no, "execution_id": execution_id,
                             "pid": None, "process_created_filetime": None,
                             "started_at": _now(), "ended_at": None, "exit_code": None,
                             "report_path": name, "report_sha256": None}
                process = spawn.Process(target=_audit_worker,
                    args=(str(root), current, method, round_no, execution_id, str(session / name)),
                    name=f"amount-{method}{round_no}-{execution_id[:8]}")
                interrupted = None
                try:
                    process.start()
                    # Protect bootstrap too: check_started is never emitted
                    # until the OS has assigned this process to the parent job.
                    job.assign(process.sentinel)
                    execution["pid"] = process.pid
                    execution["process_created_filetime"] = process_creation_filetime(process.sentinel)
                    _notify(progress, current, "check_started", **execution)
                    child_before = time.monotonic()
                    while process.is_alive():
                        if cancel is not None and cancel():
                            interrupted = "CANCELLED"
                            break
                        if time.monotonic() - child_before >= timeout:
                            interrupted = "TIMEOUT"
                            break
                        process.join(0.05)
                    if interrupted is not None and process.is_alive():
                        process.terminate()
                    process.join(10)
                    if process.is_alive():
                        process.kill()
                        process.join(10)
                    execution["exit_code"] = process.exitcode
                finally:
                    if process.pid is not None:
                        if process.is_alive():
                            process.kill()
                            process.join(10)
                        execution["exit_code"] = process.exitcode
                    process.close()
                    execution["ended_at"] = _now()
                checks.append(execution)
                if (session / name).exists():
                    try:
                        raw_report, sha = read_report_bytes(session / name)
                        execution["report_sha256"] = sha
                        report = parse_report_bytes(raw_report)
                        report_errors = validate_report(report, current, execution)
                    except Exception as error:
                        report_errors = ["报告无法读取：" + str(error)]
                else:
                    report_errors = ["子进程报告缺失"]
                if interrupted is not None:
                    status = interrupted
                    errors.append("子进程" + ("超时" if interrupted == "TIMEOUT" else "已取消"))
                elif report_errors:
                    status = "FAIL"
                    errors.extend(name + "：" + error for error in report_errors)
                else:
                    passed += 1
                _notify(progress, current, "check_finished", status="PASS" if not report_errors and interrupted is None else status,
                        **execution, checks_passed=passed)
                if interrupted is not None or report_errors:
                    break
            if errors:
                break
        if not errors:
            valid, gate_errors = validate_audit_session(root, current, checks, session)
            errors.extend(gate_errors)
            status = "PASS" if valid else "FAIL"
    except BaseException as error:
        status = "CANCELLED" if isinstance(error, KeyboardInterrupt) else "ERROR"
        errors.append(type(error).__name__ + ": " + str(error)[:2000])
    finally:
        if job is not None:
            try:
                job.close()
            except BaseException as error:
                status = "ERROR"
                errors.append("审查Job关闭失败：" + str(error))
    summary = {"summary_version": 1, "mode": current.mode, "batch_id": current.batch_id,
               "audit_id": current.audit_id, "checker_version": CHECKER_VERSION, "provenance": current.provenance,
               "expected_request_sha256": current.expected_request_sha256,
               "expected_manifest_sha256": current.expected_manifest_sha256,
               "expected": expected_statistics(current), "status": status,
               "checks_passed": passed, "checks": checks, "errors": errors,
               "started_at": started_at, "ended_at": _now(), "elapsed_seconds": time.monotonic() - before}
    summary_sha = None
    try:
        # Human-readable evidence first; machine-readable success is the last write.
        text = (f"按套金额核查 {current.batch_id}\n审查 {current.audit_id}\n状态 {status}\n"
                f"通过 {passed}/15\n" + "\n".join(errors) + "\n")
        text_bytes = text.encode('utf-8')
        synced_write(session / "summary.txt", text_bytes)
        summary['summary_text_sha256'] = digest(text_bytes)
        data = canonical_json(summary)
        synced_write(session / "summary.json", data)
        summary_sha = digest(data)
    except BaseException as error:
        status = "ERROR"
        errors.append("汇总报告写入失败：" + str(error))
    return AuditOutcome(current, status, tuple(checks), session, summary_sha, tuple(errors), passed)
