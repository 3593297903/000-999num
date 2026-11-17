"""Actual frozen-candidate soak controller. QA only; never imported by the app.

The controller retains every counted batch, report, package and command receipt.
An unknown failure stops generation. A missing/changed receipt is never replaced
by regenerating another batch. PAUSE stops new commands while monitoring continues.
"""
import argparse
from contextlib import contextmanager
import ctypes
from datetime import datetime
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

import psutil

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from tests.amount_mode.support_release import (ReleaseHarness, assert_audit, canonical, fixed_hashes,
                                               read_json, sha, write_json)

THRESHOLDS = {
    "minimum_small_batches": 100, "minimum_large_batches": 10, "minimum_real_history": 100,
    "minimum_gui_uptime_seconds": 7200, "sample_interval_seconds": 5,
    "maximum_heartbeat_gap_seconds": 10, "warmup_gui_seconds": 60,
    "idle_settling_grace_seconds": 10, "gui_growth_after_warmup_bytes": 192 * 1024**2,
    "gui_handle_growth_after_warmup": 40, "idle_task_children": 0,
    "task_peak_rss_bytes": 1536 * 1024**2, "command_timeout_seconds": 600,
}


def now():
    return datetime.now().astimezone().isoformat()


def atomic_json(path, value):
    path = Path(path)
    pending = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with pending.open("xb") as stream:
        stream.write(canonical(value)); stream.flush(); os.fsync(stream.fileno())
    os.replace(pending, path)


def append_jsonl(path, value):
    with Path(path).open("ab") as stream:
        stream.write(canonical(value)); stream.flush(); os.fsync(stream.fileno())


def normalized(value):
    return Path(value).resolve(strict=False)


def tree_bytes(root):
    total = count = 0
    root = Path(root)
    if not root.exists():
        return {"files": 0, "bytes": 0}
    for folder, directories, files in os.walk(root, followlinks=False):
        parent = Path(folder)
        directories[:] = [name for name in directories if not (parent / name).is_symlink() and not (parent / name).is_junction()]
        for name in files:
            path = parent / name
            if path.is_symlink():
                raise ValueError("QA storage contains an unexpected file link: " + str(path))
            try:
                total += path.stat().st_size; count += 1
            except FileNotFoundError:
                # A diagnostic log/cache can rotate between enumeration and stat.
                continue
    return {"files": count, "bytes": total}


def visible_windows(pid):
    """Read-only OS window diagnostic, not an automation/input mechanism."""
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    result = []
    @callback_type
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user.IsWindowVisible(hwnd):
            result.append(int(hwnd))
        return True
    if not user.EnumWindows(visit, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return result


class ProcessExitHandle:
    """Keep the real Windows process object until its exit code is collected."""
    def __init__(self, pid):
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.GetExitCodeProcess.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def exit_code(self):
        from ctypes import wintypes
        value = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return None if value.value == 259 else value.value

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def idle_probes(data_root):
    """OS lock and SQLite write-lock probe; no data row or batch is modified."""
    lock = Path(data_root) / ".coordinator.lock"
    with lock.open("r+b") as stream:
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    connection = sqlite3.connect((Path(data_root) / "history.sqlite").as_uri() + "?mode=rw",
                                 uri=True, isolation_level=None, timeout=0.2)
    try:
        connection.execute("PRAGMA busy_timeout=200")
        connection.execute("BEGIN IMMEDIATE")
        assert connection.in_transaction
        connection.rollback()
        assert not connection.in_transaction
    finally:
        connection.close()
    return {"workspace_os_lock_available": True, "sqlite_write_transaction_available": True,
            "probe_connection_closed": True}


def validate_package(package, record):
    """Independent byte/anchor check of the single counted batch's package."""
    root = normalized(package)
    marker, manifest, anchor = (read_json(root / name) for name in
                                ("BACKUP_COMPLETE.json", "backup_manifest.json", "input_anchor.json"))
    original_backup_receipt = read_json(record["receipt_path"])["backup"]
    assert normalized(original_backup_receipt["package_path"]) == root
    assert marker["backup_id"] == original_backup_receipt["backup_id"]
    assert marker["backup_manifest_sha256"] == original_backup_receipt["backup_manifest_sha256"], "Backup changed from its original completed receipt"
    assert marker["status"] == "COMPLETE" and marker["backup_version"] == manifest["backup_version"] == anchor["backup_version"] == 1
    assert marker["batch_id"] == manifest["batch_id"] == anchor["batch_id"] == record["batch_id"]
    assert marker["backup_id"] == manifest["backup_id"] == anchor["backup_id"]
    assert sha(root / "backup_manifest.json") == marker["backup_manifest_sha256"]
    assert anchor["expected_request_sha256"] == record["request_sha256"]
    assert anchor["expected_manifest_sha256"] == record["snapshot_digest"]
    assert anchor["frozen_set_amounts"] == record["amounts"]
    assert hashlib.sha256(canonical(anchor["original_request"])).hexdigest() == record["request_sha256"]
    expected_names = {"backup_manifest.json", "BACKUP_COMPLETE.json"}
    for entry in manifest["files"]:
        relative = entry["path"]
        assert type(relative) is str and "\\" not in relative and ":" not in relative
        parts = Path(relative).parts
        assert not Path(relative).is_absolute() and ".." not in parts
        assert relative not in expected_names
        expected_names.add(relative)
        path = root / relative
        assert path.resolve().is_relative_to(root) and path.is_file() and not path.is_symlink()
        assert path.stat().st_size == entry["size"] and sha(path) == entry["sha256"]
    actual_names = set()
    for folder, directories, files in os.walk(root, followlinks=False):
        parent = Path(folder)
        assert not any((parent / n).is_symlink() or (parent / n).is_junction() for n in directories)
        actual_names.update((parent / n).relative_to(root).as_posix() for n in files)
    assert actual_names == expected_names
    assert fixed_hashes(root / "batch")["manifest.json"] == record["snapshot_digest"]
    return {"package_path": str(root), "backup_id": marker["backup_id"],
            "backup_manifest_sha256": marker["backup_manifest_sha256"], "file_count": len(actual_names)}


def validate_record(record, candidate_hashes, qa_boundary):
    assert record["kind"] == "generation" and record["status"] == "PASS"
    assert record["exe_sha256"] == candidate_hashes
    for key in ("config_path", "receipt_path", "command_evidence", "batch_dir", "offline_path"):
        assert normalized(record[key]).is_relative_to(qa_boundary), "Evidence lies outside this QA boundary"
    assert sha(record["config_path"]) == record["config_sha256"]
    assert sha(record["receipt_path"]) == record["receipt_sha256"]
    assert sha(record["offline_path"]) == record["offline_sha256"]
    command = read_json(record["command_evidence"])
    assert command["status"] == "FINISHED" and command["exit_code"] == 0
    assert command["exe_sha256"] == command["exe_sha256_after"] == candidate_hashes
    assert command["idle_children"] == [] and command["resource_sample_errors"] == []
    assert command["peak_tree_rss_bytes"] <= THRESHOLDS["task_peak_rss_bytes"]
    amounts = tuple(int(value) for value in record["amounts"])
    config = read_json(record["config_path"])
    assert config["amounts"] == record["amounts"] and config["set_count"] == len(amounts)
    result = read_json(record["receipt_path"])
    assert result["status"] == "COMPLETED" and result["checks_passed"] == 15
    assert result["batch_id"] == record["batch_id"] and result["audit_id"] == record["audit_id"]
    assert result["request_sha256"] == record["request_sha256"] and result["snapshot_digest"] == record["snapshot_digest"]
    assert_audit(record["batch_dir"], result, amounts, record["dist_dir"], Path(record["dist_dir"]) / "AmountTool.exe")
    offline = read_json(record["offline_path"])
    assert offline["status"] == "PASS" and offline["reader_source_sha256"] == sha(PROJECT / "scripts/amount_offline.py")
    assert offline["original_config_sha256"] == record["config_sha256"]
    assert offline["raw_total"] == record["raw_total"] == 1000 * len(amounts)
    assert offline["unique_ids_exact"] is True and offline["source_cells_exact"] is True and offline["output_bytes_equal"] is True
    assert len(offline["source_cell_counts"]) == len(amounts)
    assert all(len(row) == 1000 and all(v == 1 for v in row) for row in offline["source_cell_counts"])
    assert len(offline["per_code_statistics"]) == 1000
    assert all(row["actual_raw_count"] == len(amounts) and row["actual_amount"] == str(sum(amounts))
               and row["raw_difference"] == 0 and row["amount_difference"] == "0" for row in offline["per_code_statistics"])
    current_files = fixed_hashes(record["batch_dir"])
    assert all(offline["fixed_files"][name]["sha256"] == current_files[name] for name in current_files)
    assert result["backup"]["status"] == "COMPLETE"
    backup = validate_package(result["backup"]["package_path"], record)
    return dict(record, reused_validation_at=now(), reused_backup_validation=backup)


class Checkpoint:
    def __init__(self, root, initial):
        self.root = root
        self.path = root / "state.json"
        self.lock = threading.RLock()
        self.value = initial
        self.update()

    def update(self, **values):
        with self.lock:
            self.value.update(values)
            self.value["updated_at"] = now()
            atomic_json(self.path, self.value)

    def get(self, key, default=None):
        with self.lock:
            return self.value.get(key, default)

    def fail(self, issue, **details):
        with self.lock:
            issues = list(self.value.get("failures", []))
            issues.append({"at": now(), "error": str(issue), **details})
            self.update(status="FAILED", needs_root_review=True, failures=issues)


class GuiMonitor(threading.Thread):
    def __init__(self, process, data_root, backup_root, state, operation_lock, initial_resource=None):
        super().__init__(name="amount-real-gui-monitor", daemon=True)
        self.process = process
        self.pid = process.pid
        self.creation = process.create_time()
        self.data_root = data_root
        self.backup_root = backup_root
        self.state = state
        self.operation_lock = operation_lock
        self.stop_event = threading.Event()
        self.samples = []
        self.settling_since = None
        self.last_heartbeat = None
        self.last_count = None
        self.last_uptime = None
        self.baseline = state.get("warmup_baseline")
        self.health_path = data_root / "logs" / "gui_health.json"
        self.first_sample_at = time.monotonic()
        self.process.cpu_percent(None)
        if initial_resource is not None:
            original = read_json(initial_resource)
            assert original["pid"] == self.pid and original["process_creation_time"] == self.creation
            self.baseline = self.baseline or {"rss_bytes": original["rss_bytes"], "private_bytes": original["private_bytes"],
                                              "handles": original["handles"], "at": original["at"], "source": str(initial_resource)}
            append_jsonl(self.state.root / "resources.jsonl", {"kind": "actual_initial_sample", **original})
            self.state.update(warmup_baseline=self.baseline)

    def health(self):
        value = read_json(self.health_path)
        assert value["pid"] == self.pid
        assert type(value["started_monotonic"]) in (int, float) and type(value["uptime_seconds"]) in (int, float)
        assert type(value["heartbeat_count"]) is int and type(value["busy"]) is bool
        gap = time.monotonic() - (value["started_monotonic"] + value["uptime_seconds"])
        assert -1 <= gap <= THRESHOLDS["maximum_heartbeat_gap_seconds"], f"GUI heartbeat gap {gap:.3f}s"
        if self.last_count is not None:
            assert value["heartbeat_count"] >= self.last_count and value["uptime_seconds"] >= self.last_uptime
        self.last_count, self.last_uptime = value["heartbeat_count"], value["uptime_seconds"]
        self.last_heartbeat = time.monotonic()
        return value

    def sample(self):
        process = psutil.Process(self.pid)
        assert process.create_time() == self.creation, "GUI PID identity changed"
        health = self.health()
        windows = visible_windows(self.pid)
        assert windows, "No visible top-level window for the actual GUI process"
        memory = process.memory_info()
        private = getattr(memory, "private", None)
        assert type(private) is int, "Actual Windows private memory could not be read"
        handles = process.num_handles()
        children = process.children(recursive=True)
        child_data = []
        tree_rss = memory.rss
        for child in children:
            try:
                info = child.memory_info()
                tree_rss += info.rss
                child_data.append({"pid": child.pid, "created": child.create_time(), "rss_bytes": info.rss, "handles": child.num_handles()})
            except psutil.NoSuchProcess:
                continue
        files = process.open_files()
        opened = [str(normalized(value.path)) for value in files]
        business = []
        sqlite_files = []
        for value in opened:
            path = Path(value)
            if path.is_relative_to(self.data_root):
                relative = path.relative_to(self.data_root).as_posix()
                if relative.startswith(("batches/", "staging/")) or relative in ("history.sqlite", "history.sqlite-journal", "history.sqlite-wal", ".coordinator.lock"):
                    business.append(value)
                if relative.startswith("history.sqlite"):
                    sqlite_files.append(value)
        idle = not health["busy"] and not self.state.get("in_flight", False)
        sample = {"at": now(), "monotonic": time.monotonic(), "pid": self.pid, "process_creation_time": self.creation,
                  "gui_health": health, "visible_window_handles": windows, "rss_bytes": memory.rss, "private_bytes": private,
                  "handles": handles, "cpu_percent_since_previous_sample": process.cpu_percent(None),
                  "children": child_data, "open_files": opened, "open_business_file_handles": business,
                  "observed_sqlite_file_handles": sqlite_files, "idle": idle, "tree_rss_bytes": tree_rss,
                  "data_storage": tree_bytes(self.data_root), "backup_storage": tree_bytes(self.backup_root),
                  "disk_free_bytes": shutil.disk_usage(self.data_root).free}
        assert tree_rss <= THRESHOLDS["task_peak_rss_bytes"], "GUI task process tree exceeds predeclared RSS threshold"
        if idle:
            if self.baseline is None and health["uptime_seconds"] >= THRESHOLDS["warmup_gui_seconds"]:
                self.baseline = {"rss_bytes": memory.rss, "private_bytes": private, "handles": handles, "at": now(), "source": "first_actual_idle_sample_after_warmup"}
                self.state.update(warmup_baseline=self.baseline)
            if self.baseline is not None:
                growth = max(memory.rss - self.baseline["rss_bytes"], private - self.baseline["private_bytes"])
                handle_growth = handles - self.baseline["handles"]
                sample.update(memory_growth_after_warmup_bytes=growth, handles_growth_after_warmup=handle_growth)
                assert growth <= THRESHOLDS["gui_growth_after_warmup_bytes"], "GUI memory growth exceeds predeclared threshold"
                assert handle_growth <= THRESHOLDS["gui_handle_growth_after_warmup"], "GUI handle growth exceeds predeclared threshold"
            issue = None
            if child_data or business:
                issue = "GUI idle children or business file handles remain"
            elif self.operation_lock.acquire(blocking=False):
                try:
                    if not self.state.get("in_flight", False):
                        sample.update(idle_probes(self.data_root))
                except (OSError, sqlite3.Error) as error:
                    issue = str(error)
                finally:
                    self.operation_lock.release()
            else:
                sample["idle_probe"] = "deferred_during_command_transition"
            if issue is not None:
                self.settling_since = self.settling_since or time.monotonic()
                sample["idle_probe"] = "settling: " + issue
                assert time.monotonic() - self.settling_since <= THRESHOLDS["idle_settling_grace_seconds"], issue
            else:
                self.settling_since = None
        else:
            self.settling_since = None
        self.samples.append(sample)
        append_jsonl(self.state.root / "resources.jsonl", sample)
        self.state.update(gui_pid=self.pid, gui_creation_time=self.creation, gui_uptime_seconds=health["uptime_seconds"],
                          gui_started_monotonic=health["started_monotonic"], gui_health=health,
                          latest_resource=sample, resource_samples=len(self.samples), resource_status="OBSERVED_OK")

    def run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    self.sample()
                except (FileNotFoundError, json.JSONDecodeError) as error:
                    # The app writes the short heartbeat every five seconds.
                    # A racing read may see a partial JSON once; no fake zero sample.
                    if time.monotonic() - (self.last_heartbeat or self.first_sample_at) > 10:
                        raise RuntimeError("Cannot read a current GUI heartbeat: " + str(error)) from error
                except psutil.NoSuchProcess:
                    if self.state.get("status") in ("READY_TO_CLOSE", "PASS"):
                        return
                    raise RuntimeError("Actual GUI exited before all soak conditions were ready")
                except AssertionError as error:
                    if self.state.get("status") in ("READY_TO_CLOSE", "PASS") and str(error).startswith("No visible top-level"):
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            if not self.process.is_running():
                                return
                            time.sleep(0.2)
                    raise
                self.stop_event.wait(THRESHOLDS["sample_interval_seconds"])
        except BaseException as error:
            self.state.fail(type(error).__name__ + ": " + str(error), phase="gui_resource_monitor")


class SoakHarness(ReleaseHarness):
    def __init__(self, *args, checkpoint, operation_lock, **kwargs):
        self.checkpoint = checkpoint
        self.operation_lock = operation_lock
        super().__init__(*args, **kwargs)

    def invoke(self, command, *args, **kwargs):
        with self.operation_lock:
            planned_evidence = self.root / "cmd" / f"{self.counter + 1:03d}-{command}" / "command.json"
            self.checkpoint.update(in_flight=True, current_phase="actual_exe_command",
                                   current_command={"command": command, "args": list(map(str, args)), "started_at": now()},
                                   current_command_evidence=str(planned_evidence), current_possible_completed_batch_id=None)
            try:
                with gui_pause(getattr(self, 'gui_control_dir', None), self.checkpoint):
                    result, evidence, directory = super().invoke(command, *args, timeout=THRESHOLDS["command_timeout_seconds"], **kwargs)
                self.checkpoint.update(last_command=str(directory / "command.json"),
                                       current_command_evidence=str(directory / "command.json"),
                                       current_possible_completed_batch_id=result.get("batch_id") if type(result) is dict else None,
                                       last_command_exit_code=evidence["exit_code"])
                assert evidence["peak_tree_rss_bytes"] <= THRESHOLDS["task_peak_rss_bytes"], "CLI task peak RSS exceeds threshold"
                return result, evidence, directory
            except BaseException:
                # Keep the exact failed command and any already-written core
                # completion receipt for reconciliation. Never point a new
                # failure at the previous successful batch or regenerate it.
                if planned_evidence.is_file():
                    evidence = read_json(planned_evidence)
                    receipt = Path(evidence["result_json"])
                    possible = None
                    if receipt.is_file():
                        actual = read_json(receipt)
                        possible = actual.get("batch_id")
                        if possible is None and type(actual.get("operation_result")) is dict:
                            possible = actual["operation_result"].get("batch_id")
                    self.checkpoint.update(last_command=str(planned_evidence), last_command_exit_code=evidence.get("exit_code"),
                                           current_possible_completed_batch_id=possible)
                raise
            finally:
                self.checkpoint.update(in_flight=False, current_phase="independent_offline_acceptance")


@contextmanager
def gui_pause(control_dir, state):
    """Coordinate only with the external QA Qt timer; no production test hook."""
    if control_dir is None:
        yield
        return
    control = Path(control_dir)
    request = control / 'PAUSE'
    content = uuid.uuid4().hex.encode('ascii')
    request.write_bytes(content)
    fingerprint = hashlib.sha256(content).hexdigest()
    deadline = time.monotonic() + 90
    try:
        while True:
            ack_file = control / 'PAUSED.json'
            try:
                ack = read_json(ack_file) if ack_file.exists() else {}
            except (json.JSONDecodeError, OSError):
                ack = {}
            if ack.get('request_sha256') == fingerprint and ack.get('busy') is False:
                state.update(last_gui_pause_ack=ack)
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('Real bundled GUI did not acknowledge QA pause')
            time.sleep(0.05)
        yield
    finally:
        if request.exists() and request.read_bytes() == content:
            request.unlink()


@contextmanager
def controller_lock(root):
    path = root / ".controller.lock"
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0"); stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def counts(records, gui_data):
    small = sum(1 <= record["set_count"] <= 10 for record in records.values())
    large = sum(record["set_count"] == 300 and record["raw_total"] == 300000 and len(set(record["amounts"])) > 1 for record in records.values())
    local = [record["batch_id"] for record in records.values() if normalized(record["batch_dir"]).parent.parent == gui_data]
    return {"small_batches": small, "large_batches": large, "all_certified_generations": len(records),
            "certified_generation_ids_in_gui_history": local, "certified_gui_history_count": len(local)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", required=True, type=Path)
    parser.add_argument("--qa-root", required=True, type=Path)
    parser.add_argument("--reuse-ledger", action="append", type=Path, default=[])
    parser.add_argument("--gui-pid", type=int)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--initial-resource-json", type=Path)
    parser.add_argument("--start-paused", action="store_true")
    parser.add_argument('--gui-control-dir', type=Path)
    args = parser.parse_args(argv)
    os.chdir(PROJECT)
    qa = args.qa_root.resolve(strict=True)
    boundary = (PROJECT / "qa/amount_mode").resolve()
    assert qa != boundary and qa.is_relative_to(boundary), "Only this run's independent QA root is allowed"
    root = qa / "soak"
    root.mkdir(exist_ok=True)
    with controller_lock(root):
        return run(args, qa, root, boundary)


def run(args, qa, root, boundary):
    previous = read_json(root / "state.json") if (root / "state.json").exists() else {}
    if previous.get("status") == "PASS":
        print("This controller already recorded PASS; no batch was regenerated.", flush=True)
        return 0
    if previous.get("needs_root_review") or previous.get("status") == "FAILED":
        print("Previous failure requires root review; no new generation was started: " + str(root / "state.json"), flush=True)
        return 3
    dist = args.dist_dir.resolve(strict=True)
    exe_hashes = {name: sha(dist / name) for name in ("AmountTool.exe", "AmountVerify.exe")}
    if previous:
        assert previous["exe_sha256"] == exe_hashes, "Candidate changed; previous soak cannot certify this candidate"
        assert not previous.get("in_flight"), "An interrupted in-flight command must be reconciled, never regenerated blindly"
    data = normalized(args.data_dir or previous.get("gui_data_root") or root / "data")
    assert data.is_relative_to(qa), "The actual GUI root must belong to this QA run"
    gui_pid = args.gui_pid or previous.get("gui_pid")
    gui_process = None
    if gui_pid is None:
        gui_stdout = (root / "gui-stdout.log").open("ab")
        gui_stderr = (root / "gui-stderr.log").open("ab")
        try:
            gui_process = subprocess.Popen([str(dist / "AmountTool.exe"), "gui", "--data-dir", str(data)],
                                           cwd=dist, stdout=gui_stdout, stderr=gui_stderr)
        finally:
            gui_stdout.close(); gui_stderr.close()
        gui_pid = gui_process.pid
    process = psutil.Process(gui_pid)
    assert normalized(process.exe()) == dist / "AmountTool.exe", "PID does not belong to the selected actual candidate"
    creation = process.create_time()
    exit_handle = ProcessExitHandle(gui_pid)
    if previous.get("gui_creation_time") is not None:
        assert previous["gui_creation_time"] == creation, "Continuous GUI process identity changed"
    actual_args = process.cmdline()
    if args.gui_control_dir:
        control = args.gui_control_dir.resolve(strict=True)
        assert control.is_relative_to(qa), 'GUI control belongs to this QA run only'
        startup = read_json(control / 'child-start.json')
        ready = read_json(control / 'READY.json')
        assert startup['pid'] == ready['pid'] == gui_pid
        assert normalized(startup['exe']) == dist / 'AmountTool.exe'
        assert startup['exe_sha256'] == exe_hashes['AmountTool.exe']
        assert normalized(startup['data_root']) == normalized(ready['data_root']) == data
        from ctypes import wintypes
        times = [wintypes.FILETIME() for _ in range(4)]
        exit_handle.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        assert exit_handle.kernel.GetProcessTimes(exit_handle.handle, *[ctypes.byref(t) for t in times])
        exact_created = times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
        assert exact_created == startup['os_identity']['creation_filetime'], 'Exact GUI process creation identity changed'
    else:
        assert "gui" in actual_args and "--data-dir" in actual_args
        assert normalized(actual_args[actual_args.index("--data-dir") + 1]) == data
    ledgers = list(dict.fromkeys([*previous.get("ledger_paths", []), *map(str, args.reuse_ledger)]))
    initial = {**previous, "state_version": 1, "status": "RUNNING", "needs_root_review": False,
               "controller_pid": os.getpid(), "controller_creation_time": psutil.Process().create_time(),
               "controller_command": [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
               "controller_source_sha256": sha(__file__), "helper_source_sha256": sha(PROJECT / "tests/amount_mode/support_release.py"),
               "exe_sha256": exe_hashes, "dist_dir": str(dist), "gui_pid": gui_pid, "gui_creation_time": creation,
               "gui_data_root": str(data), "thresholds": THRESHOLDS, "ledger_paths": ledgers, "in_flight": False,
               "started_at": previous.get("started_at", now()), "current_phase": "validating_explicit_receipts"}
    state = Checkpoint(root, initial)
    operation_lock = threading.Lock()
    monitor = GuiMonitor(process, data, Path(str(data) + "_backups"), state, operation_lock, args.initial_resource_json)
    monitor.start()
    if args.start_paused:
        (root / "PAUSE").touch(exist_ok=True)
    records = {}
    ledger_signatures = {}
    original_lines = {}

    def refresh_ledgers():
        for ledger in ledgers:
            ledger_path = normalized(ledger)
            assert ledger_path.is_relative_to(boundary), "Ledger lies outside this new mode QA area"
            stat = ledger_path.stat()
            signature = stat.st_size, stat.st_mtime_ns
            if ledger_signatures.get(str(ledger_path)) == signature:
                continue
            data_bytes = ledger_path.read_bytes()
            # A producer may currently be appending one new receipt. Count only
            # complete LF-terminated lines and revisit that ledger next time.
            complete = not data_bytes or data_bytes.endswith(b"\n")
            if not complete:
                data_bytes = data_bytes[:data_bytes.rfind(b"\n") + 1]
            for raw in data_bytes.splitlines():
                record = json.loads(raw)
                if record.get("kind") != "generation" or record.get("status") != "PASS":
                    continue
                key = str(ledger_path), record.get("batch_id")
                fingerprint = hashlib.sha256(raw).hexdigest()
                assert key not in original_lines or original_lines[key] == fingerprint, "A previously read ledger line changed"
                original_lines[key] = fingerprint
                if record.get("exe_sha256") != exe_hashes:
                    append_jsonl(root / "ignored-evidence.jsonl", {"ledger": str(ledger_path), "batch_id": record.get("batch_id"),
                                                                 "reason": "different_candidate_hashes_not_counted"})
                    continue
                existing = records.get(record["batch_id"])
                if existing is not None:
                    assert existing["request_sha256"] == record["request_sha256"] and existing["snapshot_digest"] == record["snapshot_digest"]
                    continue
                valid = validate_record(record, exe_hashes, boundary)
                records[valid["batch_id"]] = valid
                append_jsonl(root / "validated-reuse.jsonl", {"ledger": str(ledger_path), "batch_id": valid["batch_id"], "validated_at": now()})
            if complete:
                ledger_signatures[str(ledger_path)] = signature

    try:
        refresh_ledgers()
        harness = SoakHarness(dist, root, checkpoint=state, operation_lock=operation_lock)
        harness.gui_control_dir = args.gui_control_dir.resolve(strict=True) if args.gui_control_dir else None
        harness.data, harness.backups = data, Path(str(data) + "_backups")
        harness.ledger.touch(exist_ok=False)
        ledgers.append(str(harness.ledger))
        state.update(ledger_paths=list(dict.fromkeys(ledgers)), counts=counts(records, data), current_phase="paused" if (root / "PAUSE").exists() else "idle")
        last_refresh = time.monotonic()
        was_paused = (root / "PAUSE").exists()
        while True:
            if state.get("status") == "FAILED":
                raise RuntimeError("Resource monitor failed; inspect state.json")
            paused = (root / "PAUSE").exists()
            if time.monotonic() - last_refresh >= 15 or (was_paused and not paused):
                refresh_ledgers()
                last_refresh = time.monotonic()
            was_paused = paused
            current = counts(records, data)
            state.update(counts=current, paused=paused)
            health = state.get("gui_health")
            if health is None or paused or health["busy"]:
                time.sleep(1)
                continue
            if current["small_batches"] < THRESHOLDS["minimum_small_batches"]:
                size = current["small_batches"] % 10 + 1
                amounts = tuple((current["small_batches"] + i) % 11 + 1 for i in range(size))
                label = f"small-{current['small_batches'] + 1:03d}"
            elif current["large_batches"] < THRESHOLDS["minimum_large_batches"]:
                amounts = (tuple(10**100 - 1 - i for i in range(300)) if current['large_batches'] == 9 else
                           tuple((i + current["large_batches"]) % 7 + 1 for i in range(300)))
                label = f"large-{current['large_batches'] + 1:02d}"
            else:
                assert current["certified_gui_history_count"] >= THRESHOLDS["minimum_real_history"], "Selected GUI history has fewer than 100 independently certified real batches"
                if health["uptime_seconds"] >= THRESHOLDS["minimum_gui_uptime_seconds"]:
                    latest = state.get("latest_resource", {})
                    if latest.get("idle") and latest.get("workspace_os_lock_available") and latest.get("sqlite_write_transaction_available"):
                        break
                state.update(current_phase="continuous_gui_observation_counts_complete")
                time.sleep(1)
                continue
            state.update(current_phase="generating_and_checking", current_label=label, current_amounts=[str(a) for a in amounts])
            value = harness.generate(label, amounts)
            record = value["record"]
            assert record["batch_id"] not in records
            backup = validate_package(value["result"]["backup"]["package_path"], record)
            record["soak_backup_validation"] = backup
            records[record["batch_id"]] = record
            append_jsonl(root / "accepted-batches.jsonl", {"batch_id": record["batch_id"], "audit_id": record["audit_id"],
                                                          "label": label, "ledger": str(harness.ledger), "backup_validation": backup, "at": now()})
            with operation_lock:
                with gui_pause(harness.gui_control_dir, state):
                    proof = idle_probes(data)
            state.update(counts=counts(records, data), last_batch_id=record["batch_id"], last_batch_idle_proof=proof,
                         current_phase="idle", current_label=None)
            print(json.dumps({"at": now(), "counts": counts(records, data), "gui_uptime_seconds": state.get("gui_uptime_seconds"),
                              "last_batch_id": record["batch_id"], "state_file": str(state.path)}, ensure_ascii=False), flush=True)
            # Let the same real GUI browse history/results between CLI tasks.
            if harness.gui_control_dir:
                time.sleep(1)
        # Do not close a visible desktop here. Root completes the CUA handoff,
        # observes Alt-F4/normal exit, and this controller records actual exit 0.
        state.update(current_phase="final_actual_receipt_revalidation")
        for record in records.values():
            validate_record(record, exe_hashes, boundary)
        assert state.get("status") != "FAILED", "Resource monitoring failed during final evidence verification"
        state.update(status="READY_TO_CLOSE", current_phase="awaiting_actual_gui_normal_exit", counts=counts(records, data))
        print("READY_TO_CLOSE: actual GUI has met uptime/count/resource gates; close it normally using the desktop tool.", flush=True)
        while exit_handle.exit_code() is None:
            if state.get("status") == "FAILED":
                raise RuntimeError("Resource monitoring failed before normal close")
            time.sleep(1)
        exit_code = exit_handle.exit_code()
        assert exit_code == 0, f"Actual GUI exit code was {exit_code}, not normal 0"
        assert {name: sha(dist / name) for name in exe_hashes} == exe_hashes
        monitor.stop_event.set()
        monitor.join(timeout=15)
        assert not monitor.is_alive() and state.get("status") != "FAILED", "Resource monitor must finish successfully before PASS"
        state.update(status="PASS", needs_root_review=False, gui_exit_code=exit_code, current_phase="finished",
                     counts=counts(records, data), completed_at=now(), all_counted_batches_retained=True)
        return 0
    except BaseException as error:
        state.fail(type(error).__name__ + ": " + str(error), phase=state.get("current_phase"),
                   possible_completed_batch_id=state.get("current_possible_completed_batch_id"))
        print("FAILED; no replacement batch generated: " + str(error), file=sys.stderr, flush=True)
        return 3
    finally:
        monitor.stop_event.set()
        monitor.join(timeout=15)
        exit_handle.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(type(error).__name__ + ": " + str(error), file=sys.stderr, flush=True)
        raise SystemExit(3)
