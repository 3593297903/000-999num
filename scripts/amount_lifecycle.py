"""QA-only actual-exe cancellation and hard-exit probes; never a production flag."""
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
from tests.amount_mode.support_release import (ReleaseHarness, assert_audit, fixed_hashes,
                                               read_json, sha, write_json)


def console_pids():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    kernel.GetConsoleProcessList.restype = wintypes.DWORD
    values = (wintypes.DWORD * 128)()
    count = kernel.GetConsoleProcessList(values, 128)
    if not count or count > 128:
        raise ctypes.WinError(ctypes.get_last_error())
    return list(values[:count])


def identity(process, exe):
    assert Path(process.exe()).resolve() == Path(exe).resolve()
    return {"pid": process.pid, "created": process.create_time(), "exe": process.exe()}


def resources(process):
    memory = process.memory_info()
    return {"pid": process.pid, "created": process.create_time(), "rss_bytes": memory.rss,
            "private_bytes": getattr(memory, "private", None), "handles": process.num_handles(),
            "open_files": [item.path for item in process.open_files()]}


def os_job_membership(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        value = wintypes.BOOL()
        if not kernel.IsProcessInJob(handle, None, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(value.value)
    finally:
        kernel.CloseHandle(handle)


def row(root, bid):
    with closing(sqlite3.connect((Path(root) / "history.sqlite").as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return dict(connection.execute("SELECT * FROM batches WHERE batch_id=?", (bid,)).fetchone())


def partial_files(batch):
    result = {}
    for name in ("request.json", "group_plan.json", "items.jsonl", "groups.txt", "manifest.json"):
        path = Path(batch) / name
        result[name] = {"exists": path.exists(), "size": path.stat().st_size if path.exists() else None,
                        "sha256": sha(path) if path.is_file() else None}
    return result


def wait_children_gone(instances, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        alive = []
        for item in instances:
            try:
                child = psutil.Process(item["pid"])
                if child.create_time() == item["created"] and child.is_running():
                    alive.append(item)
            except psutil.NoSuchProcess:
                pass
        if not alive or time.monotonic() >= deadline:
            return alive
        time.sleep(0.05)


def execute_probe(payload):
    """Observe actual flushed product logs; target only this owned process handle."""
    tool, root, directory = Path(payload["tool"]), Path(payload["data"]), Path(payload["directory"])
    cancel = payload["case"].startswith("cancel-")
    console_before = console_pids() if cancel else None
    if cancel:
        allowed = {os.getpid()}
        for ancestor in psutil.Process().parents():
            if ancestor.pid == payload["console_controller_pid"]:
                break
            allowed.add(ancestor.pid)
        write_json(directory / "isolated-console.json", {"console_pids": console_before, "allowed_private_launcher_chain": sorted(allowed),
                                                          "outer_controller_pid": payload["console_controller_pid"]})
        assert os.getpid() in console_before and payload["console_controller_pid"] not in console_before
        assert set(console_before).issubset(allowed), "Cancellation helper must own an isolated console/launcher chain"
    args = [str(tool), "generate", "--config", payload["config"], "--data-dir", str(root),
            "--backup-dir", payload["backup"], "--result-json", str(directory / "generate.json")]
    info = {"case": payload["case"], "command": args, "expected_exe_sha256": payload["exe_sha256"],
            "status": "RUNNING", "console_before": console_before}
    write_json(directory / "probe.json", info)
    started = time.monotonic()
    log = root / "logs" / "amount.log"
    cursor = 0
    pending = b""
    observed = None
    bid = None
    descendants = []
    with (directory / "stdout.log").open("wb") as output, (directory / "stderr.log").open("wb") as error:
        process = subprocess.Popen(args, stdout=output, stderr=error, cwd=tool.parent,
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if cancel else 0)
        parent = psutil.Process(process.pid)
        info["identity"] = identity(parent, tool)
        info["initial_resources"] = resources(parent)
        write_json(directory / "probe.json", info)
        assert sha(tool) == payload["exe_sha256"]
        try:
            while process.poll() is None and time.monotonic() - started < 300:
                if log.exists():
                    with log.open("rb") as stream:
                        stream.seek(cursor); data = stream.read(); cursor = stream.tell()
                    pending += data
                    lines = pending.split(b"\n"); pending = lines.pop()
                    for raw in lines:
                        brace = raw.find(b"{")
                        if brace < 0:
                            continue
                        event = json.loads(raw[brace:])
                        if event.get("event") != "job_progress":
                            continue
                        bid = event.get("batch_id", bid)
                        if event.get("state") == payload["boundary"] or event.get("phase") == payload["boundary"]:
                            observed = event
                if observed is not None:
                    if payload["case"] == "crash-write":
                        details = root / "staging" / bid / "items.jsonl"
                        if not details.is_file() or details.stat().st_size == 0 or (details.parent / "manifest.json").exists():
                            time.sleep(0.001); continue
                    assert parent.create_time() == info["identity"]["created"]
                    identity(parent, tool)
                    # Freeze the exact observed parent before diagnostic I/O.
                    # Otherwise a short commit can legitimately finish while
                    # psutil enumerates handles and the kill would be too late.
                    parent.suspend()
                    info["parent_suspended_at_observed_boundary"] = True
                    descendants = [identity(child, tool) for child in parent.children(recursive=True)]
                    for child in descendants:
                        try: child["is_process_in_any_os_job"] = os_job_membership(child["pid"])
                        except OSError: child["is_process_in_any_os_job"] = None
                    info.update(observed_event=observed, batch_id=bid, observed_elapsed_seconds=time.monotonic() - started,
                                state_before_action=row(root, bid), descendants_before_action=descendants,
                                resources_before_action=resources(parent))
                    if payload["case"] in ("cancel-after", "crash-publish"):
                        assert info["state_before_action"]["generation_status"] == "COMMITTING", "Commit observation was too late; not a valid boundary test"
                    if cancel:
                        attached = console_pids()
                        assert os.getpid() in attached and process.pid in attached
                        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                        kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
                        kernel.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
                        # CTRL_BREAK to the owned child group, never global 0.
                        assert process.pid > 0
                        if not kernel.GenerateConsoleCtrlEvent(1, process.pid):
                            raise ctypes.WinError(ctypes.get_last_error())
                        info.update(action="CTRL_BREAK_EVENT", target_process_group=process.pid, console_at_signal=attached)
                        parent.resume()
                    else:
                        process.kill()  # Popen holds this exact OS process handle.
                        info["action"] = "TerminateProcess_owned_parent_handle"
                    break
                time.sleep(0.001)
            assert observed is not None and "action" in info, "Actual requested boundary was not reached"
            code = process.wait(timeout=600)
        except BaseException as issue:
            if process.poll() is None:
                process.kill(); process.wait(timeout=15)
            info.update(status="FAILED", error=type(issue).__name__ + ": " + str(issue), exit_code=process.returncode,
                        traceback=traceback.format_exc())
            write_json(directory / "probe.json", info)
            raise
    alive = wait_children_gone(descendants)
    info.update(status="OBSERVED", exit_code=code, elapsed_seconds=time.monotonic() - started,
                descendants_remaining=alive, state_after_exit=row(root, bid), exe_sha256_after=sha(tool))
    batch = root / "batches" / bid
    if not batch.exists(): batch = root / "staging" / bid
    info["batch_dir_after_exit"] = str(batch)
    info["fixed_files_after_exit"] = partial_files(batch)
    write_json(directory / "probe.json", info)
    assert not alive, "Actual audit children remained after the 15-second cleanup deadline: " + repr(alive)
    assert info["exe_sha256_after"] == payload["exe_sha256"], "Candidate executable changed during the case"
    return info


def run_case(harness, case):
    directory = harness.root / case
    directory.mkdir()
    root = directory / "d"
    config = directory / "input.json"
    # Enough time to observe actual writes/commit, without repeating maximum
    # scale work that has its own release/soak acceptance.
    size = 100 if case in ("crash-publish", "cancel-after") else 10
    amounts = tuple(i % 7 + 1 for i in range(size))
    write_json(config, {"set_count": size, "amounts": list(map(str, amounts))})
    boundaries = {"cancel-before": "check_started", "cancel-after": "COMMITTING", "crash-write": "WRITING",
                  "crash-audit": "check_started", "crash-publish": "PUBLISHED"}
    payload = {"tool": str(harness.tool), "data": str(root), "directory": str(directory), "case": case,
               "config": str(config), "backup": str(directory / "bk"), "boundary": boundaries[case],
               "exe_sha256": harness.exe_sha256["AmountTool.exe"]}
    if case.startswith("cancel-"):
        payload["console_controller_pid"] = os.getpid()
        payload_path = directory / "console-payload.json"
        write_json(payload_path, payload)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        command = [sys.executable, str(Path(__file__).resolve()), "--_worker-payload", str(payload_path)]
        with (directory / "console-worker.log").open("wb") as stream:
            worker = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, startupinfo=startup,
                                    creationflags=subprocess.CREATE_NEW_CONSOLE, timeout=900)
        write_json(directory / "console-worker-command.json", {"command": command, "exit_code": worker.returncode,
                                                               "isolated_new_console": True, "window_hidden": True})
        assert worker.returncode == 0, "Console helper failed: " + str(directory / "console-worker.log")
        info = read_json(directory / "probe.json")
    else:
        info = execute_probe(payload)
    bid = info["batch_id"]
    old_files = info["fixed_files_after_exit"]
    if case == "cancel-before":
        assert info["exit_code"] == 5 and read_json(directory / "generate.json")["status"] == "CANCELLED"
        assert info["state_after_exit"]["generation_status"] == "CANCELLED"
        assert not (root / "batches" / bid).exists()
    elif case == "cancel-after":
        assert info["exit_code"] == 0 and info["state_after_exit"]["generation_status"] == "COMPLETED"
        result = read_json(directory / "generate.json")
        assert result["status"] == "COMPLETED"
        assert_audit(result["batch_dir"], result, amounts, harness.dist, harness.tool)
    else:
        assert info["exit_code"] != 0 and info["exit_code"] != 5
        assert info["state_after_exit"]["generation_status"] != "COMPLETED"
        if case == "crash-write":
            assert info["state_after_exit"]["manifest_sha256"] is None
            rejected, _, _ = harness.invoke("recover", "--batch-id", bid, "--data-dir", root,
                                             "--backup-dir", directory / "bk", expected_exit=3)
            assert rejected["status"] == "ERROR" and partial_files(info["batch_dir_after_exit"]) == old_files
        else:
            old_audits = {p.name for p in (Path(info["batch_dir_after_exit"]) / "audits").iterdir()} if (Path(info["batch_dir_after_exit"]) / "audits").exists() else set()
            recovered, _, evidence = harness.invoke("recover", "--batch-id", bid, "--data-dir", root,
                                                     "--backup-dir", directory / "bk", timeout=900)
            assert recovered["status"] == "COMPLETED" and recovered["checks_passed"] == 15
            assert recovered["audit_id"] not in old_audits
            assert partial_files(recovered["batch_dir"]) == old_files
            audited = assert_audit(recovered["batch_dir"], recovered, amounts, harness.dist, harness.tool)
            backup, _, _ = harness.invoke("backup", "--batch-id", bid, "--data-dir", root,
                                           "--backup-dir", directory / "bk", "--destination", directory / "bk")
            assert backup["status"] in ("COMPLETE", "REUSED")
            info.update(recovery_receipt=recovered, recovery_audit=audited, recovery_command=str(evidence / "command.json"), backup=backup)
    info["acceptance_status"] = "PASS"
    write_json(directory / "accepted.json", info)
    print(json.dumps({"case": case, "status": "PASS", "actual_exit": info["exit_code"], "batch_id": bid,
                      "evidence": str(directory / "accepted.json")}, ensure_ascii=False), flush=True)
    return info


def normal_cycles(harness):
    seed = harness.generate("startup-seed", (1, 5, 2))
    result = seed["result"]
    before = fixed_hashes(result["batch_dir"])
    k = result["group_count"]
    plans = [("history", ["--offset", 0, "--limit", 1]), ("history", ["--offset", 0, "--limit", 30]),
             ("history", ["--offset", 1, "--limit", 30]), ("version-info", [])]
    plans += [("group", ["--batch-id", result["batch_id"], "--group", number])
              for number in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, k // 2, k - 1, k)]
    observed = []
    for name, args in plans:
        output, command, evidence = harness.invoke(name, *args, "--data-dir", harness.data, "--backup-dir", harness.backups)
        assert output["status"] in ("OK", "SUPPORTED")
        assert fixed_hashes(result["batch_dir"]) == before
        observed.append({"command": name, "pid": command["pid"], "exit_code": command["exit_code"],
                         "command_evidence": str(evidence / "command.json"), "idle_children": command["idle_children"],
                         "peak_tree_rss_bytes": command["peak_tree_rss_bytes"], "peak_tree_handles": command["peak_tree_handles"]})
    assert len(observed) == 17 and all(item["exit_code"] == 0 and not item["idle_children"] for item in observed)
    write_json(harness.root / "normal-startup-cycles.json", {"status": "PASS", "actual_normal_cycles": len(observed),
                                                            "cycles": observed, "exe_sha256": harness.exe_sha256,
                                                            "seed_receipt": seed["record"]["receipt_path"]})
    return observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path)
    parser.add_argument("--qa-root", type=Path)
    parser.add_argument("--case", default="priority", choices=("priority", "all-faults", "normal", "final", "remaining", "cancel-before", "cancel-after", "crash-write", "crash-audit", "crash-publish"))
    parser.add_argument("--reuse-cycle-report", action="append", default=[], type=Path)
    parser.add_argument("--_worker-payload", type=Path)
    args = parser.parse_args()
    os.chdir(PROJECT)
    if args._worker_payload:
        execute_probe(read_json(args._worker_payload))
        return 0
    assert args.dist_dir and args.qa_root
    qa = args.qa_root.resolve(strict=True)
    assert qa.is_relative_to((PROJECT / "qa/amount_mode").resolve())
    harness = ReleaseHarness(args.dist_dir, qa)
    harness.manifest["lifecycle_source_sha256"] = sha(__file__)
    write_json(harness.root / "candidate.json", harness.manifest)
    cases = ("cancel-before", "crash-publish", "cancel-after") if args.case == "priority" else (("crash-publish", "cancel-before", "cancel-after") if args.case == "remaining" else (
        ("crash-write", "crash-audit", "crash-publish", "cancel-before", "cancel-after") if args.case in ("all-faults", "final") else (() if args.case == "normal" else (args.case,)))
    )
    results = []
    try:
        reused = []
        for file in args.reuse_cycle_report:
            accepted = read_json(file)
            source_manifest = read_json(file.parent.parent / "candidate.json")
            assert source_manifest["exe_sha256"] == harness.exe_sha256
            assert accepted["acceptance_status"] == "PASS" and accepted["case"] in ("crash-write", "crash-audit", "crash-publish")
            assert accepted["expected_exe_sha256"] == harness.exe_sha256["AmountTool.exe"]
            assert accepted["state_after_exit"]["generation_status"] != "COMPLETED" and accepted["descendants_remaining"] == []
            if "recovery_receipt" in accepted:
                receipt = accepted["recovery_receipt"]
                assert partial_files(receipt["batch_dir"]) == accepted["fixed_files_after_exit"]
                values = tuple(map(int, read_json(file.parent / "input.json")["amounts"]))
                assert_audit(receipt["batch_dir"], receipt, values, harness.dist, harness.tool)
            reused.append({"case": accepted["case"], "evidence": str(file.resolve()), "sha256": sha(file), "original_qa_source_sha256": source_manifest.get("lifecycle_source_sha256")})
        for case in cases:
            results.append(run_case(harness, case))
        normal = normal_cycles(harness) if args.case in ("normal", "final", "remaining") else []
        hard = len({info["case"] for info in results if info["case"].startswith("crash-")} | {info["case"] for info in reused})
        write_json(harness.root / "lifecycle-summary.json", {"status": "PASS_FOR_EXECUTED_CASES", "cases": [r["case"] for r in results],
                                                              "exe_sha256": harness.exe_sha256,
                                                              "normal_cycles": len(normal), "hard_exit_cycles": hard,
                                                              "reused_same_candidate_cycles": reused,
                                                              "minimum_20_startup_exit_gate": len(normal) + hard >= 20 and hard >= 3,
                                                              "remaining": ["version_migration_rollback"] + ([] if len(normal) + hard >= 20 and hard >= 3 else ["20_real_startup_exit_cycles"]),
                                                              "root": str(harness.root)})
        return 0
    except BaseException as error:
        write_json(harness.root / "lifecycle-summary.json", {"status": "FAILED", "executed_passed_cases": [r["case"] for r in results],
                                                              "error": type(error).__name__ + ": " + str(error),
                                                              "traceback": traceback.format_exc(),
                                                              "exe_sha256": harness.exe_sha256, "root": str(harness.root)})
        print(type(error).__name__ + ": " + str(error), file=sys.stderr, flush=True)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
