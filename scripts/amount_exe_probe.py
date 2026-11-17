"""QA-only frozen-checker probe, never imported by the packaged application.

Normal complete fixtures run A/B/C five times each.  Four deliberately invalid
fixtures run each checker once by default (or five times with --failure-rounds
5).  These are negative-test candidates, never completed native batches.  Only
this external QA parent can reseal their candidate manifests or construct the
worker context; no such option is added to the application's CLI or GUI.

The source parent temporarily uses CPython's frozen Windows spawn command.  The
candidate entry point must preload its bundled audit/protocol/ABC modules before
freeze_support, so parent preparation data cannot redirect those modules to the
source checkout.  Every report's origins are checked against the actual OS image.
"""
import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import asdict
import hashlib
import importlib.util
import json
import math
import multiprocessing
import multiprocessing.spawn
import os
from pathlib import Path
import sys
import time
import uuid


FIXED_FILES = ("request.json", "group_plan.json", "items.jsonl", "groups.txt", "manifest.json")
COMMON_PROOFS = (
    "request_matches_frozen_input", "snapshot_matches_trusted_hashes", "source_code_cells_exact",
    "raw_per_code_exact", "per_record_amount_exact", "group_sizes_exact", "group_code_amounts_exact",
    "final_per_code_amount_exact", "final_total_amount_exact", "final_format_canonical",
)
EXTRA_PROOFS = {"A": (), "B": ("standard_records_equal",),
                "C": ("unique_ids_exact", "output_bytes_equal")}
CHECKER_FILES = {"A": "a_count", "B": "b_sort", "C": "c_ids"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _pairs(pairs):
    output = {}
    for key, value in pairs:
        require(key not in output, "duplicate JSON key: " + key)
        output[key] = value
    return output


def _bad_constant(value):
    raise ValueError("nonstandard JSON constant: " + value)


def strict_json(data):
    return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_bad_constant)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())


def fixed_hashes(root):
    return {name: {"size": (root / name).stat().st_size, "sha256": sha256((root / name).read_bytes())}
            for name in FIXED_FILES}


def manifest_facts(root, context, *, request_must_match):
    fingerprints = fixed_hashes(root)
    manifest_bytes = (root / "manifest.json").read_bytes()
    manifest = strict_json(manifest_bytes)
    require(canonical(manifest) == manifest_bytes, "QA manifest is not canonical")
    require(sha256(manifest_bytes) == context.expected_manifest_sha256, "QA candidate manifest context differs")
    entries = manifest["files"]
    require(type(entries) is list and len(entries) == 4, "QA manifest file collection")
    seen = set()
    for entry in entries:
        name = entry["path"]
        require(name in FIXED_FILES[:4] and name not in seen, "QA manifest paths")
        seen.add(name)
        require(type(entry["size"]) is int and entry["size"] == fingerprints[name]["size"], "QA manifest sizes")
        require(entry["sha256"] == fingerprints[name]["sha256"], "QA manifest data hash")
    request_matches = fingerprints["request.json"]["sha256"] == context.expected_request_sha256
    require(request_matches is request_must_match, "unexpected original request fingerprint relation")
    return {"fixed_files": fingerprints, "candidate_manifest_self_consistent": True,
            "original_request_fingerprint_matches_actual": request_matches}


def _records(root):
    return [strict_json(line) for line in (root / "items.jsonl").read_bytes().splitlines()]


def _write_records(root, records):
    with (root / "items.jsonl").open("wb") as stream:
        for row in records:
            stream.write(canonical(row))


def _qa_text_from_records(root, records):
    """Only constructs negative QA data; never used by application or checkers."""
    groups = {}
    for row in records:
        values = groups.setdefault(row["group"], {})
        values[row["code"]] = values.get(row["code"], 0) + int(row["amount"])
    pieces = []
    for group, values in groups.items():
        lines = [".".join(code for code in values if values[code] == amount) + "直选" + str(amount) + "元"
                 for amount in sorted(set(values.values()))]
        pieces.append(f"第{group}组\n" + "\n".join(lines))
    (root / "groups.txt").write_bytes(("\n\n".join(pieces) + "\n").encode("utf-8"))


def _margins(root, set_count):
    """Demonstrate which deliberately insufficient summaries the attacks preserve."""
    rows = _records(root)
    sources, raw_codes, final_codes = [0] * set_count, [0] * 1000, [0] * 1000
    raw_money = 0
    for row in rows:
        sources[row["set_no"] - 1] += 1
        raw_codes[int(row["code"])] += 1
        raw_money += int(row["amount"])
    group_totals, final_money = [], 0
    for block in (root / "groups.txt").read_text(encoding="utf-8").rstrip("\n").split("\n\n"):
        group_money = 0
        for line in block.split("\n")[1:]:
            codes, amount = line.split("直选")
            value = int(amount[:-1])
            for code in codes.split("."):
                final_codes[int(code)] += value
                final_money += value
                group_money += value
        group_totals.append(str(group_money))
    return {"raw_total": len(rows), "per_set_raw_counts": sources, "raw_per_code": raw_codes,
            "final_per_code": [str(value) for value in final_codes],
            "raw_total_amount": str(raw_money), "final_total_amount": str(final_money),
            "group_output_totals": group_totals}


def _context_record(context):
    value = asdict(context)
    value["frozen_set_amounts"] = [str(amount) for amount in context.frozen_set_amounts]
    return value


def prepare_cases(run, fixture, helpers):
    cases = []
    for number, fixture_case in enumerate(fixture["cases"], 1):
        name = f"normal-{number:02d}"
        root = run / "candidates" / name
        amounts = tuple(map(int, fixture_case["set_amounts"]))
        leading = (tuple(row["item_id"] for row in fixture_case["records"]),)
        context = helpers.candidate(root, amounts, leading)
        first = (root / "groups.txt").read_bytes().split(b"\n\n", 1)[0]
        require(first == ("第1组\n" + fixture_case["expected_body"].rstrip("\n")).encode("utf-8"),
                "manual fixture body differs before exe checks: " + name)
        cases.append({"id": name, "name": fixture_case["name"], "root": root, "context": context,
                      "expected": "PASS", "facts": manifest_facts(root, context, request_must_match=True)})

    adversarial = {case["id"]: case for case in fixture["adversarial_cases"]}
    ids = ("balanced_equal_amount_source_duplicates", "balanced_group_amount_swap",
           "replace_all_000_no_total_change", "reanchored_input_self_consistent")
    for name in ids:
        specification = adversarial[name]
        amounts = tuple(map(int, specification.get("set_amounts", specification.get("trusted_set_amounts"))))
        root = run / "candidates" / name
        leading = tuple(tuple(group) for group in specification.get("leading_groups", ()))
        original_context = helpers.candidate(root, amounts, leading)
        original_request = (root / "request.json").read_bytes()
        before = _margins(root, len(amounts))
        records = _records(root)
        if name == "balanced_equal_amount_source_duplicates":
            replacements = {item["from_item_id"]: item["to_item_id"] for item in specification["replacements"]}
            for row in records:
                if row["item_id"] in replacements:
                    item_id = replacements[row["item_id"]]
                    row.update(item_id=item_id, set_no=item_id // 1000 + 1, code=f"{item_id % 1000:03d}",
                               amount=str(amounts[item_id // 1000]))
            _write_records(root, records)
        elif name == "balanced_group_amount_swap":
            pieces = (root / "groups.txt").read_text(encoding="utf-8").split("\n\n")
            for index in range(2):
                require(pieces[index] == f"第{index + 1}组\n" + specification["normal_bodies"][index].rstrip("\n"),
                        "normal leading group differs from manual attack fixture")
                pieces[index] = f"第{index + 1}组\n" + specification["mutated_bodies"][index].rstrip("\n")
            (root / "groups.txt").write_bytes("\n\n".join(pieces).encode("utf-8"))
        elif name == "replace_all_000_no_total_change":
            replacements = {item["from_item_id"]: item["to_item_id"] for item in specification["replacements"]}
            for row in records:
                if row["item_id"] in replacements:
                    item_id = replacements[row["item_id"]]
                    row.update(item_id=item_id, set_no=item_id // 1000 + 1, code=f"{item_id % 1000:03d}",
                               amount=str(amounts[item_id // 1000]))
            _write_records(root, records)
            _qa_text_from_records(root, records)
        else:
            replacement = tuple(map(int, specification["replacement_set_amounts"]))
            request = strict_json(original_request)
            for row, amount in zip(request["sets"], replacement):
                row["amount"] = str(amount)
            (root / "request.json").write_bytes(canonical(request))
            for row in records:
                row["amount"] = str(replacement[row["set_no"] - 1])
            _write_records(root, records)
            _qa_text_from_records(root, records)
        # This is the only test-only trust adjustment: candidate manifest hash.
        # The original request hash and strict frozen tuple are never replaced.
        context = helpers.reseal(root, original_context)
        require(context.frozen_set_amounts == amounts
                and context.expected_request_sha256 == original_context.expected_request_sha256,
                "QA mutation must not replace original input or original request hash")
        after = _margins(root, len(amounts))
        if name in ("balanced_equal_amount_source_duplicates", "balanced_group_amount_swap"):
            require(before == after, "balanced attack unexpectedly changed a preserved margin")
        elif name == "replace_all_000_no_total_change":
            for key in ("raw_total", "per_set_raw_counts", "raw_total_amount", "final_total_amount"):
                require(before[key] == after[key], "missing-000 attack changed preserved " + key)
        facts = manifest_facts(root, context, request_must_match=name != "reanchored_input_self_consistent")
        facts.update(original_context=_context_record(original_context),
                     original_request_utf8=original_request.decode("utf-8"),
                     margins_before=before, margins_after=after, qa_manifest_resealed=True)
        cases.append({"id": name, "name": specification["name"], "root": root, "context": context,
                      "expected": "REJECT", "facts": facts})
    for case in cases:
        write_json(run / "contexts" / (case["id"] + ".json"), {
            "case_id": case["id"], "expected": case["expected"], "context": _context_record(case["context"]),
            "facts": case["facts"], "not_a_completed_native_batch": True})
    return cases


@contextmanager
def frozen_windows_launcher(executable):
    """Temporary QA-parent adaptation of CPython 3.13's documented frozen branch."""
    original_executable = multiprocessing.spawn.get_executable()
    original_command = multiprocessing.spawn.get_command_line
    commands = []
    def command_line(**keywords):
        require(set(keywords) == {"parent_pid", "pipe_handle"}
                and all(type(value) is int and value > 0 for value in keywords.values()),
                "unexpected Windows multiprocessing command arguments")
        command = [str(executable), "--multiprocessing-fork"] + ["%s=%r" % item for item in keywords.items()]
        commands.append(command)
        return command
    multiprocessing.set_executable(str(executable))
    multiprocessing.spawn.get_command_line = command_line
    try:
        yield commands
    finally:
        multiprocessing.spawn.get_command_line = original_command
        multiprocessing.set_executable(original_executable)


def os_process_identity(handle):
    """Query the actual child process handle, not a claimed report or PID alone."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    process_handle = wintypes.HANDLE(handle)
    buffer, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
    if not kernel.QueryFullProcessImageNameW(process_handle, 0, buffer, ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
    if not kernel.GetProcessTimes(process_handle, ctypes.byref(created), ctypes.byref(exited),
                                  ctypes.byref(kernel_time), ctypes.byref(user_time)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"executable": str(Path(buffer.value).resolve(strict=True)),
            "creation_filetime": (created.dwHighDateTime << 32) | created.dwLowDateTime}


def validate_worker_report(report, execution, context, method, round_no, expected, executable):
    errors = []
    identity = {"report_version": 1, "mode": context.mode, "batch_id": context.batch_id,
                "audit_id": context.audit_id, "checker_version": context.checker_version,
                "execution_id": execution["execution_id"], "method": method, "round": round_no,
                "provenance": context.provenance, "expected_request_sha256": context.expected_request_sha256,
                "expected_manifest_sha256": context.expected_manifest_sha256,
                "pid": execution["pid"], "process_created_filetime": execution["os_identity"]["creation_filetime"]}
    for name, value in identity.items():
        if type(report.get(name)) is not type(value) or report.get(name) != value:
            errors.append("report identity/type mismatch: " + name)
    runtime = report.get("runtime")
    try:
        require(type(runtime) is dict and runtime.get("frozen") is True, "report is not a frozen worker")
        require(type(runtime.get("executable")) is str
                and Path(runtime["executable"]).resolve(strict=True) == executable,
                "reported executable differs from candidate")
        require(Path(execution["os_identity"]["executable"]) == executable, "OS process image differs from candidate")
        bundle = Path(runtime["bundle_root"]).resolve(strict=True)
        require(bundle.is_dir() and bundle.is_relative_to(executable.parent), "worker bundle is outside candidate folder")
        module = Path(runtime["checker_module"]).resolve(strict=False)
        require(module.is_relative_to(bundle) and module.stem == CHECKER_FILES[method],
                "checker module did not originate in the candidate bundle")
    except (KeyError, OSError, ValueError, TypeError) as error:
        errors.append("runtime origin: " + str(error))
    calculated = {"set_count": len(context.frozen_set_amounts), "raw_total": len(context.frozen_set_amounts) * 1000,
                  "group_count": context.expected_group_count, "per_code_amount": str(sum(context.frozen_set_amounts)),
                  "total_amount": str(sum(context.frozen_set_amounts) * 1000)}
    if type(report.get("expected")) is not dict:
        errors.append("expected statistics missing")
    else:
        for name, value in calculated.items():
            if type(report["expected"].get(name)) is not type(value) or report["expected"].get(name) != value:
                errors.append("expected statistics mismatch: " + name)
    if expected == "PASS":
        actual = {"actual_raw_total": calculated["raw_total"], "actual_group_count": calculated["group_count"],
                  "actual_total_amount": calculated["total_amount"], "checked_codes": 1000,
                  "checked_source_code_cells": calculated["raw_total"], "files_read": 5}
        if execution["exit_code"] != 0 or report.get("status") != "PASS" or report.get("errors") != []:
            errors.append("valid fixture did not pass and exit normally")
        for name, value in actual.items():
            if type(report.get(name)) is not type(value) or report.get(name) != value:
                errors.append("actual statistic mismatch: " + name)
        proofs = report.get("proofs")
        if type(proofs) is not dict or any(proofs.get(name) is not True for name in (*COMMON_PROOFS, *EXTRA_PROOFS[method])):
            errors.append("required actual proof absent/false")
    else:
        # A crash, timeout, missing report or import error is not a successful
        # negative test: require the checker's actual data rejection (exit 3).
        if execution["exit_code"] != 3 or report.get("status") != "FAIL":
            errors.append("invalid fixture was not rejected by the checker with exit 3")
        if type(report.get("errors")) is not list or not report["errors"]:
            errors.append("rejection has no actual diagnostic")
        elif not all(type(message) is str and message.startswith("ValueError:") for message in report["errors"]):
            errors.append("negative test ended with an infrastructure exception")
    return errors


def run_worker(worker, commands, executable, case, method, round_no, report_path, timeout, execution):
    context = case["context"]
    execution_id = uuid.uuid4().hex
    process = multiprocessing.get_context("spawn").Process(
        target=worker, args=(str(case["root"]), context, method, round_no, execution_id, str(report_path)),
        name=f"qa-bundled-{method}{round_no}-{execution_id[:8]}")
    execution.update(method=method, round=round_no, execution_id=execution_id, pid=None,
                     exit_code=None, os_identity=None, timed_out=False, report_path=str(report_path))
    started, command_index = time.monotonic(), len(commands)
    try:
        process.start()
        execution["pid"] = process.pid
        execution["os_identity"] = os_process_identity(process.sentinel)
        require(Path(execution["os_identity"]["executable"]) == executable,
                "actual subprocess is not the requested candidate exe")
        while process.is_alive() and time.monotonic() - started < timeout:
            process.join(0.05)
        if process.is_alive():
            execution["timed_out"] = True
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
        execution["elapsed_seconds"] = time.monotonic() - started
        execution["actual_spawn_commands"] = commands[command_index:]
    require(not execution["timed_out"], "candidate worker timed out; not an expected data rejection")
    require(report_path.is_file() and not report_path.is_symlink() and not report_path.is_junction(),
            "actual candidate report missing or linked")
    require(report_path.stat().st_size <= 65536, "candidate report exceeds protocol limit")
    raw = report_path.read_bytes()
    report = strict_json(raw)
    require(type(report) is dict and canonical(report) == raw, "candidate report not a canonical JSON object")
    execution["report_sha256"] = sha256(raw)
    execution["report_status"] = report.get("status")
    execution["checker_errors"] = report.get("errors")
    execution["runtime"] = report.get("runtime")
    execution["validation_errors"] = validate_worker_report(
        report, execution, context, method, round_no, case["expected"], executable)
    # Stop promptly on an origin/bootstrap problem instead of repeating it 132 times.
    require(not any(error.startswith("runtime origin:") for error in execution["validation_errors"]),
            "candidate frozen origin check failed: " + "; ".join(execution["validation_errors"]))
    return execution


def _helpers(project):
    path = project / "tests" / "amount_mode" / "helpers.py"
    spec = importlib.util.spec_from_file_location("amount_external_qa_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True)
    parser.add_argument("--qa-root", required=True)
    parser.add_argument("--failure-rounds", type=int, choices=(1, 5), default=1,
                        help="negative-test repetitions per checker; normal fixtures always use five")
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args(argv)
    project = Path(__file__).resolve().parents[1]
    run = None
    summary = {"status": "FAIL", "evidence_kind": "external_QA_probe_of_actual_bundled_checkers",
               "batches_marked_completed": 0, "cases": [], "errors": []}
    try:
        require(os.name == "nt" and not getattr(sys, "frozen", False), "run this QA parent with source Python on Windows")
        require(math.isfinite(args.timeout_seconds) and args.timeout_seconds > 0, "invalid worker timeout")
        executable = Path(args.exe).resolve(strict=True)
        require(executable.is_file() and executable.suffix.lower() == ".exe", "candidate exe missing")
        qa_root = Path(args.qa_root).resolve(strict=False)
        require(qa_root.is_relative_to((project / "qa" / "amount_mode").resolve(strict=True)),
                "QA probe root must be inside this project's isolated qa/amount_mode")
        for ancestor in (qa_root, *qa_root.parents):
            require(not ((ancestor / "request.json").exists()
                         and any((ancestor / name).exists() for name in ("manifest.json", "items.jsonl", "group_plan.json", "quotas.json"))),
                    "QA probe root must not lie inside an existing batch")
        qa_root.mkdir(parents=True, exist_ok=True)
        run = qa_root / ("exe-probe-" + uuid.uuid4().hex[:10])
        run.mkdir()
        fixture_path = project / "按套金额随机小组_验收样例.json"
        fixture_bytes = fixture_path.read_bytes()
        fixture = strict_json(fixture_bytes)
        require(fixture["fixture_version"] == 2 and fixture["handoff_version"] == "1.1"
                and len(fixture["cases"]) == 8, "requires complete fixture_version=2 / handoff 1.1")
        # Parent-only imports: a frozen child rerunning __mp_main__ executes only
        # the stdlib definitions above, then unpickles the preloaded bundled target.
        from number_app.amount_mode.audit import _audit_worker
        helper_module = _helpers(project)
        cases = prepare_cases(run, fixture, helper_module)
        candidate_hash = sha256(executable.read_bytes())
        summary.update(exe=str(executable), exe_sha256=candidate_hash, qa_run=str(run),
                       source_parent_executable=sys.executable,
                       harness_sha256=sha256(Path(__file__).read_bytes()),
                       fixture_sha256=sha256(fixture_bytes), helper_sha256=sha256(Path(helper_module.__file__).read_bytes()),
                       normal_rounds_per_method=5, failure_rounds_per_method=args.failure_rounds)
        with frozen_windows_launcher(executable) as commands:
            for case in cases:
                context = case["context"]
                session = case["root"] / "audits" / context.audit_id
                session.mkdir(parents=True)
                before = fixed_hashes(case["root"])
                checks = []
                rounds = 5 if case["expected"] == "PASS" else args.failure_rounds
                for round_no in range(1, rounds + 1):
                    for method in "ABC":
                        report_path = session / f"{method}_{round_no}.json"
                        execution = {}
                        try:
                            run_worker(_audit_worker, commands, executable, case, method, round_no,
                                       report_path, args.timeout_seconds, execution)
                        finally:
                            # Keep OS identity/argv/exit evidence even when bootstrap,
                            # report availability or bundle-origin checks fail.
                            write_json(run / "executions" / case["id"] / f"{method}_{round_no}.json", execution)
                        checks.append(execution)
                errors = [error for execution in checks for error in execution["validation_errors"]]
                if fixed_hashes(case["root"]) != before:
                    errors.append("candidate fixed files changed during actual exe checks")
                if sha256(executable.read_bytes()) != candidate_hash:
                    errors.append("candidate exe changed during probe")
                for execution in checks:
                    if sha256(Path(execution["report_path"]).read_bytes()) != execution["report_sha256"]:
                        errors.append("actual report changed before probe summary")
                item = {"id": case["id"], "name": case["name"], "expected": case["expected"],
                        "probe_status": "PASS" if not errors else "FAIL", "checks": checks, "errors": errors,
                        "actual_process_instances": len(checks), "fixed_files": before,
                        "context": _context_record(context), "facts": case["facts"]}
                write_json(run / "case-summaries" / (case["id"] + ".json"), item)
                summary["cases"].append(item)
                summary["errors"].extend(case["id"] + ": " + error for error in errors)
                print(json.dumps({"case": case["id"], "probe_status": item["probe_status"],
                                  "actual_process_instances": len(checks), "qa_run": str(run)}, ensure_ascii=False), flush=True)
        identities = [(check["pid"], check["os_identity"]["creation_filetime"])
                      for case in summary["cases"] for check in case["checks"]]
        require(len(identities) == len(set(identities)), "a process instance was reused")
        summary["actual_process_instances"] = len(identities)
        summary["normal_candidate_count"] = sum(case["expected"] == "PASS" for case in summary["cases"])
        summary["negative_candidate_count"] = sum(case["expected"] == "REJECT" for case in summary["cases"])
        require(summary["normal_candidate_count"] == 8 and summary["negative_candidate_count"] == 4,
                "required fixture collection incomplete")
        summary["status"] = "PASS" if not summary["errors"] else "FAIL"
    except (Exception, KeyboardInterrupt) as error:
        summary["errors"].append(type(error).__name__ + ": " + str(error))
    if run is not None:
        write_json(run / "summary.json", summary)
    print(json.dumps({"status": summary["status"], "qa_run": str(run) if run else None,
                      "errors": summary["errors"]}, ensure_ascii=False), flush=True)
    return 0 if summary["status"] == "PASS" else 3


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
