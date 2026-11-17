"""External source QA parent for syscall exceptions inside actual candidate exe."""
import argparse
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time
import uuid


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True)
    parser.add_argument("--qa-root", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args(argv)
    project = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project))
    from scripts.amount_exe_probe import frozen_windows_launcher, os_process_identity
    from tests.amount_mode.frozen_io_probe import run_frozen_io
    need = lambda condition: None if condition else (_ for _ in ()).throw(ValueError("source Windows parent and isolated QA required"))
    need(os.name == "nt" and not getattr(sys, "frozen", False))
    executable = Path(args.exe).resolve(strict=True)
    qa = Path(args.qa_root).resolve(strict=False)
    need(qa.is_relative_to((project / "qa" / "amount_mode").resolve(strict=True)))
    qa.mkdir(parents=True, exist_ok=True)
    run = qa / ("io-probe-" + uuid.uuid4().hex[:10]); run.mkdir()
    expected_sha = hashlib.sha256(executable.read_bytes()).hexdigest()
    result = {"status": "FAIL", "exe": str(executable), "exe_sha256": expected_sha, "run_dir": str(run), "errors": [],
              "fault_injection_kind": "in-process QA syscall exception", "real_disk_not_filled": True}
    child = multiprocessing.get_context("spawn").Process(target=run_frozen_io, args=(str(run),), name="qa-real-bundled-IO")
    started = time.monotonic()
    try:
        with frozen_windows_launcher(executable) as commands:
            child.start(); result["pid"] = child.pid
            result["os_identity"] = os_process_identity(child.sentinel)
            need(Path(result["os_identity"]["executable"]) == executable)
            print(json.dumps({"phase": "real_frozen_IO_target_started", "pid": child.pid, "run_dir": str(run)}), flush=True)
            child.join(args.timeout_seconds)
            if child.is_alive(): child.terminate(); child.join(10); raise TimeoutError("frozen IO target timeout")
            result["exit_code"] = child.exitcode; result["actual_spawn_commands"] = commands
        report = json.loads((run / "io-result.json").read_bytes())
        result["io_result_sha256"] = hashlib.sha256((run / "io-result.json").read_bytes()).hexdigest()
        if result["exit_code"] != 0 or report["status"] != "PASS": raise ValueError("actual candidate IO probe failed: " + str(report["errors"]))
        need(hashlib.sha256(executable.read_bytes()).hexdigest() == expected_sha)
        result["status"] = "PASS"
    except BaseException as error:
        result["errors"].append(type(error).__name__ + ": " + str(error))
    finally:
        if child.pid is not None and child.is_alive(): child.kill(); child.join(10)
        child.close(); result["elapsed_seconds"] = time.monotonic() - started
        (run / "parent-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "run_dir": str(run), "errors": result["errors"]}, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "PASS" else 3


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
