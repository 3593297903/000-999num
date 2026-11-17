"""Launch an isolated real candidate-exe GUI for internal Qt software testing.

This is not native desktop mouse automation.  Only a new QA child window and its
own data root are used.  Top-level imports remain stdlib for frozen spawn safety.
"""
import argparse
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time
import uuid
from datetime import datetime


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True)
    parser.add_argument("--qa-root", required=True)
    parser.add_argument("--scenario", choices=("one", "full", "soak"), default="one")
    parser.add_argument("--timeout-seconds", type=float, default=None)
    parser.add_argument("--resume-run", help="Reuse this probe's actual completed data; never generate matching batches again")
    parser.add_argument("--data-dir", help="Explicit independent QA data root, useful for a monitored soak")
    args = parser.parse_args(argv)
    project = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project))
    from scripts.amount_exe_probe import frozen_windows_launcher, os_process_identity
    from tests.amount_mode.frozen_gui_probe import run_frozen_gui
    executable = Path(args.exe).resolve(strict=True)
    qa = Path(args.qa_root).resolve(strict=False)
    if os.name != "nt" or getattr(sys, "frozen", False): raise ValueError("Source Python Windows parent required")
    if not qa.is_relative_to((project / "qa" / "amount_mode").resolve(strict=True)):
        raise ValueError("Use isolated project qa/amount_mode root")
    qa.mkdir(parents=True, exist_ok=True)
    run = qa / ("gui-probe-" + uuid.uuid4().hex[:10]); run.mkdir()
    data = Path(args.data_dir).resolve(strict=False) if args.data_dir else (Path(args.resume_run).resolve(strict=True) / "data" if args.resume_run else run / "data")
    if not data.is_relative_to((project / "qa" / "amount_mode").resolve(strict=True)):
        raise ValueError("QA GUI data must stay in isolated qa/amount_mode")
    timeout = args.timeout_seconds if args.timeout_seconds is not None else (10800 if args.scenario == "soak" else 300)
    exe_sha = hashlib.sha256(executable.read_bytes()).hexdigest()
    result = {"status": "FAIL", "exe": str(executable), "exe_sha256": exe_sha, "run_dir": str(run),
              "interaction_kind": "internal Qt controls/events; not native physical mouse input", "errors": []}
    process = multiprocessing.get_context("spawn").Process(target=run_frozen_gui,
        args=(str(run), args.scenario, str(data)), name="qa-actual-bundled-GUI")
    before = time.monotonic()
    try:
        with frozen_windows_launcher(executable) as commands:
            process.start()
            result["pid"] = process.pid
            result["os_identity"] = os_process_identity(process.sentinel)
            result["data_root"] = str(data)
            result["started_at"] = datetime.now().astimezone().isoformat()
            if Path(result["os_identity"]["executable"]) != executable:
                raise ValueError("Actual GUI child image differs from candidate")
            (run / "child-start.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"phase": "actual_GUI_child_started", "pid": process.pid, "run_dir": str(run)}), flush=True)
            last = before
            while process.is_alive() and time.monotonic() - before < timeout:
                process.join(0.1)
                if time.monotonic() - last >= 30:
                    progress = json.loads((run / "progress.json").read_bytes()) if (run / "progress.json").exists() else {}
                    print(json.dumps({"elapsed_seconds": time.monotonic() - before, "progress": progress}), flush=True)
                    last = time.monotonic()
            if process.is_alive():
                process.terminate(); process.join(10)
                raise TimeoutError("Actual bundled GUI probe timed out")
            result["exit_code"] = process.exitcode
            result["actual_spawn_commands"] = commands
        if not (run / "gui-result.json").is_file(): raise ValueError("GUI child report missing")
        report = json.loads((run / "gui-result.json").read_bytes())
        result["gui_result_sha256"] = hashlib.sha256((run / "gui-result.json").read_bytes()).hexdigest()
        result["gui_result"] = report
        if result["exit_code"] != 0 or report["status"] != "PASS": raise ValueError("Actual GUI child did not pass: " + str(report["errors"]))
        if hashlib.sha256(executable.read_bytes()).hexdigest() != exe_sha: raise ValueError("Candidate exe changed during GUI test")
        result["status"] = "PASS"
    except BaseException as error:
        result["errors"].append(type(error).__name__ + ": " + str(error))
    finally:
        if process.pid is not None and process.is_alive(): process.kill(); process.join(10)
        process.close()
        result["elapsed_seconds"] = time.monotonic() - before
        (run / "parent-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "run_dir": str(run), "errors": result["errors"]}, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "PASS" else 3


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
