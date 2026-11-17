"""One focused actual restore-many CTRL_BREAK probe, in a private QA console."""
import argparse
from contextlib import closing
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid
import psutil

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from scripts.amount_lifecycle import console_pids, identity, resources, wait_children_gone
from tests.amount_mode.support_release import read_json, sha, tree_fingerprint, write_json


def rows(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT batch_id,generation_status,latest_verification_status,manifest_sha256 FROM batches")]


def worker(payload):
    root = Path(payload["root"])
    target = root / "target"
    tool = Path(payload["tool"])
    attached = console_pids()
    allowed = {os.getpid()}
    for ancestor in psutil.Process().parents():
        if ancestor.pid == payload["controller_pid"]: break
        allowed.add(ancestor.pid)
    assert os.getpid() in attached and payload["controller_pid"] not in attached and set(attached).issubset(allowed)
    command = [str(tool), "restore-many", "--package", *payload["packages"], "--data-dir", str(target),
               "--backup-dir", str(root / "bk"), "--result-json", str(root / "actual-result.json")]
    evidence = {"command": command, "private_console_before": attached, "exe_sha256": payload["exe_sha256"],
                "script_sha256": sha(__file__), "status": "RUNNING"}
    write_json(root / "probe.json", evidence)
    with (root / "stdout.log").open("wb") as output, (root / "stderr.log").open("wb") as error:
        process = subprocess.Popen(command, stdout=output, stderr=error, cwd=tool.parent,
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        parent = psutil.Process(process.pid)
        evidence["identity"] = identity(parent, tool)
        observed = None
        before = []
        descendants = []
        start = time.monotonic()
        try:
            while process.poll() is None and time.monotonic() - start < 120:
                if (target / "history.sqlite").is_file():
                    try:
                        current = rows(target / "history.sqlite")
                    except sqlite3.DatabaseError:
                        current = []
                    if any(row["generation_status"] == "AUDITING" for row in current) and parent.children(recursive=True):
                        parent.suspend()
                        before = rows(target / "history.sqlite")
                        assert any(row["generation_status"] == "AUDITING" for row in before)
                        descendants = [identity(child, tool) for child in parent.children(recursive=True)]
                        evidence.update(state_before_signal=before, descendants=descendants, resources=resources(parent),
                                        parent_suspended_before_signal=True)
                        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                        kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
                        kernel.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
                        attached_now = console_pids()
                        assert process.pid > 0 and process.pid in attached_now and os.getpid() in attached_now
                        if not kernel.GenerateConsoleCtrlEvent(1, process.pid): raise ctypes.WinError(ctypes.get_last_error())
                        evidence.update(signal="CTRL_BREAK_EVENT", target_group=process.pid, console_at_signal=attached_now)
                        observed = True
                        parent.resume()
                        break
                time.sleep(0.001)
            assert observed, "Actual pre-COMMIT AUDITING state was not reached"
            code = process.wait(timeout=180)
        except BaseException:
            if process.poll() is None: process.kill(); process.wait(timeout=15)
            evidence.update(status="FAILED_PROBE", error=traceback.format_exc(), exit_code=process.returncode)
            write_json(root / "probe.json", evidence)
            raise
    after = rows(target / "history.sqlite")
    result = read_json(root / "actual-result.json")
    completed_before = {r["batch_id"] for r in before if r["generation_status"] == "COMPLETED"}
    completed_after = {r["batch_id"] for r in after if r["generation_status"] == "COMPLETED"}
    remaining = wait_children_gone(descendants)
    conforming = code == 5 and result.get("status") == "CANCELLED" and completed_after.issubset(completed_before) and not remaining
    evidence.update(status="PASS" if conforming else "OBSERVED_CONTRACT_FAILURE", exit_code=code, actual_result=result,
                    state_after_exit=after, descendants_remaining=remaining, expected_exit_code=5, expected_status="CANCELLED",
                    no_new_false_completion=completed_after.issubset(completed_before), exe_sha256_after=sha(tool))
    write_json(root / "probe.json", evidence)
    print(json.dumps({"status": evidence["status"], "actual_exit": code, "actual_status": result.get("status"), "evidence": str(root / "probe.json")}), flush=True)
    return 0 if conforming else 3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path)
    parser.add_argument("--qa-root", type=Path)
    parser.add_argument("--version-report", type=Path)
    parser.add_argument("--worker", type=Path)
    args = parser.parse_args()
    os.chdir(PROJECT)
    if args.worker: return worker(read_json(args.worker))
    assert args.qa_root and args.dist_dir and args.version_report
    qa = args.qa_root.resolve(strict=True)
    assert qa.is_relative_to((PROJECT / "qa/amount_mode").resolve())
    report = read_json(args.version_report)
    root = qa / ("bulk-cancel-" + uuid.uuid4().hex[:6]); root.mkdir()
    tool = args.dist_dir.resolve() / "AmountTool.exe"
    packages = [report["package_A"]["package_path"], report["package_B"]["package_path"]]
    before = [tree_fingerprint(path) for path in packages]
    payload = {"root": str(root), "tool": str(tool), "packages": packages, "controller_pid": os.getpid(), "exe_sha256": sha(tool)}
    write_json(root / "payload.json", payload)
    startup = subprocess.STARTUPINFO(); startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW; startup.wShowWindow = 0
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(root / "payload.json")]
    with (root / "console-worker.log").open("wb") as output:
        result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, startupinfo=startup,
                                creationflags=subprocess.CREATE_NEW_CONSOLE, timeout=240)
    assert [tree_fingerprint(path) for path in packages] == before
    write_json(root / "worker-command.json", {"command": command, "exit_code": result.returncode, "private_console": True,
                                             "original_packages_unchanged": True, "exe_sha256": payload["exe_sha256"]})
    print(json.dumps({"root": str(root), "worker_exit": result.returncode, "probe": str(root / "probe.json")}), flush=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
