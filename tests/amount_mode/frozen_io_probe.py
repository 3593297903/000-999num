"""QA-only syscall fault target; stdlib imports only until bundle path restore."""
import errno
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import traceback


FIXED = ("request.json", "group_plan.json", "items.jsonl", "groups.txt", "manifest.json")


def need(value, message):
    if not value: raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprints(batch):
    return {name: {"size": (batch / name).stat().st_size, "sha256": sha(batch / name)} for name in FIXED}


def save(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n"); stream.flush(); os.fsync(stream.fileno())


def setup_bundle():
    need(getattr(sys, "frozen", False) is True and hasattr(sys, "_MEIPASS"), "real frozen exe required")
    bundle = Path(sys._MEIPASS).resolve(strict=True)
    prefix = [str(bundle), str(bundle / "base_library.zip")]
    sys.path[:] = prefix + [path for path in sys.path if path not in prefix]
    names = ("number_app.amount_mode.service", "number_app.amount_mode.artifacts", "number_app.amount_mode.catalog",
             "number_app.amount_mode.recovery", "number_app.amount_mode.backup", "number_app.amount_mode.export",
             "number_app.amount_mode.audit", "number_app.amount_mode.checks.a_count", "number_app.amount_mode.checks.b_sort",
             "number_app.amount_mode.checks.c_ids")
    origins = {}
    for name in names:
        module = importlib.import_module(name)
        origin = Path(module.__file__).resolve(strict=False)
        need(origin.is_relative_to(bundle), "source fallback forbidden: " + name)
        origins[name] = str(origin)
    return bundle, origins


def anchors(row):
    return {name: row[name] for name in ("original_request_json", "frozen_input_json", "request_sha256", "manifest_sha256")}


class DiskFullStream:
    """Delegate genuine writes, then fail before the 33rd JSONL record write."""
    def __init__(self, stream, evidence):
        self.stream, self.evidence = stream, evidence
    def __enter__(self):
        self.stream.__enter__(); return self
    def __exit__(self, *args): return self.stream.__exit__(*args)
    def __getattr__(self, name): return getattr(self.stream, name)
    def write(self, data):
        if self.evidence["successful_record_writes"] >= 32:
            self.evidence["fault_raised"] = True
            raise OSError(errno.ENOSPC, "QA-only simulated disk full after 32 actual item writes")
        actual = self.stream.write(data)
        self.evidence["successful_record_writes"] += 1
        if self.evidence["successful_record_writes"] == 32:
            self.stream.flush(); os.fsync(self.stream.fileno())
            self.evidence["first_32_records_flushed_and_fsynced"] = True
        return actual


def disk_full_case(run):
    from number_app.amount_mode.service import generate_amount_batch
    from number_app.amount_mode.recovery import recover_amount_batch
    from number_app.amount_mode.catalog import Catalog
    data, backup = run / "disk-full-data", run / "disk-full-backups"
    selected = (data / "staging").resolve(strict=False)
    evidence = {"successful_record_writes": 0, "fault_raised": False, "wrapped_paths": []}
    original_open = Path.open
    def fault_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        stream = original_open(path, *args, **kwargs)
        if (path.name == "items.jsonl" and any(flag in mode for flag in "wax")
                and path.resolve(strict=False).is_relative_to(selected)):
            evidence["wrapped_paths"].append(str(path))
            return DiskFullStream(stream, evidence)
        return stream
    caught = None
    Path.open = fault_open
    try:
        generate_amount_batch((1,), data, backup_root=backup)
    except OSError as error:
        caught = {"type": type(error).__name__, "errno": error.errno, "message": str(error)}
    finally:
        Path.open = original_open
    need(caught is not None and caught["errno"] == errno.ENOSPC and evidence["fault_raised"], "actual generator did not encounter selected ENOSPC")
    catalog = Catalog(data)
    need(catalog.count() == 1 and catalog.count("COMPLETED") == 0, "disk failure created completed/replacement batch")
    summary = catalog.list_page()[0]; bid = summary["batch_id"]; row = catalog.get(bid)
    batch = catalog.path(bid)
    items = batch / "items.jsonl"
    actual_lines = items.read_bytes().splitlines()
    need(items.is_file() and len(actual_lines) == 32 and evidence["successful_record_writes"] == 32,
         "real partial items file not exactly 32 written records")
    need(row["manifest_sha256"] is None and not (batch / "manifest.json").exists(), "partial candidate was trusted/frozen")
    need(not list((data / "batches").glob("*")), "partial batch published formally")
    original = anchors(row)
    refusal = None
    try:
        recover_amount_batch(data, bid, backup_root=backup)
    except ValueError as error:
        refusal = type(error).__name__ + ": " + str(error)
    need(refusal is not None, "partial recovery was not refused")
    after = catalog.get(bid)
    need(anchors(after) == original and after["generation_status"] != "COMPLETED"
         and catalog.count() == 1 and catalog.count("COMPLETED") == 0, "recovery re-anchored/completed/regenerated partial batch")
    result = {"status": "PASS", "case": "disk_full_during_candidate_write", "fault_injection_kind": "in-process QA syscall exception",
              "physical_disk_filled": False, "data_root": str(data), "batch_id": bid, "actual_exception": caught,
              "fault_evidence": evidence, "partial_items": {"path": str(items), "sha256": sha(items), "actual_lines": len(actual_lines)},
              "trusted_manifest": row["manifest_sha256"], "generation_status_before_recovery": row["generation_status"],
              "generation_status_after_recovery": after["generation_status"], "recovery_refusal": refusal,
              "original_anchors_preserved": True, "completed_batches": 0, "batch_count": catalog.count()}
    save(run / "disk-full-result.json", result)
    return result


def inspect_actual_fifteen(catalog, bid, bundle):
    from number_app.amount_mode.protocol import COMMON_PROOFS, EXTRA_PROOFS
    context = catalog.context(bid); row = catalog.get(bid); batch = catalog.path(bid)
    audit = catalog.audit_record(row["latest_audit_id"])
    need(audit["status"] == "PASS" and len(audit["checks"]) == 15, "complete actual audit missing")
    checks, combinations, processes = [], set(), set()
    for execution in audit["checks"]:
        method, number = execution["method"], execution["round"]
        combinations.add((method, number)); path = batch / execution["report_relpath"]
        actual = json.loads(path.read_bytes()); runtime = actual["runtime"]
        need(execution["exit_code"] == 0 and actual["status"] == "PASS" and actual["errors"] == [], "actual worker failed")
        need(sha(path) == execution["report_sha256"] and actual["pid"] == execution["pid"]
             and actual["process_created_filetime"] == int(execution["process_created_filetime"]), "report hash/OS identity mismatch")
        need(runtime["frozen"] is True and Path(runtime["executable"]).resolve() == Path(sys.executable).resolve()
             and Path(runtime["checker_module"]).resolve(strict=False).is_relative_to(bundle), "worker did not use actual bundle")
        expected = {"actual_raw_total": len(context.frozen_set_amounts) * 1000,
                    "actual_group_count": context.expected_group_count, "actual_total_amount": str(sum(context.frozen_set_amounts) * 1000),
                    "checked_codes": 1000, "checked_source_code_cells": len(context.frozen_set_amounts) * 1000, "files_read": 5}
        need(all(type(actual.get(key)) is type(value) and actual.get(key) == value for key, value in expected.items()), "actual worker statistics mismatch")
        need(all(actual["proofs"].get(key) is True for key in (*COMMON_PROOFS, *EXTRA_PROOFS[method])), "actual proof missing")
        processes.add((actual["pid"], actual["process_created_filetime"]))
        checks.append({"method": method, "round": number, "pid": execution["pid"], "process_created_filetime": execution["process_created_filetime"],
                       "runtime": runtime, "report_path": str(path), "report_sha256": execution["report_sha256"], "exit_code": execution["exit_code"]})
    need(combinations == {(method, number) for method in "ABC" for number in range(1, 6)} and len(processes) == 15, "fifteen process instances/combinations not exact")
    return {"audit_id": audit["audit_id"], "checks_passed": 15, "checks": checks}


def backup_failure_case(run, bundle):
    from number_app.amount_mode import backup as module
    from number_app.amount_mode.service import generate_amount_batch
    from number_app.amount_mode.catalog import Catalog
    data, destination = run / "backup-failure-data", run / "device-absent-backups"
    resolved = destination.resolve(strict=False)
    original_copy = module._copy_verified
    injected, baseline = [], {}
    def copy_fault(source, target, *args, **kwargs):
        if Path(target).resolve(strict=False).is_relative_to(resolved):
            injected.append({"source": str(source), "target": str(target)})
            raise OSError(errno.ENODEV, "QA-only simulated backup device unavailable")
        return original_copy(source, target, *args, **kwargs)
    def progress(event):
        if event.get("state") == "COMPLETED":
            baseline.update(fingerprints(Path(event["result"]["batch_dir"])))
    module._copy_verified = copy_fault
    try:
        result = generate_amount_batch((1, 5, 2), data, backup_root=destination, progress=progress)
    finally:
        module._copy_verified = original_copy
    catalog = Catalog(data); bid = result["batch_id"]; row = catalog.get(bid); batch = catalog.path(bid)
    need(injected and result["status"] == "COMPLETED" and result["checks_passed"] == 15, "backup fault lost actual core completion")
    need(result["backup"]["status"] == "FAILED" and row["backup_status"] == "FAILED", "backup failure claimed success")
    need(row["generation_status"] == "COMPLETED" and row["latest_verification_status"] == "PASS"
         and catalog.count() == catalog.count("COMPLETED") == 1, "backup failure regenerated/rolled back batch")
    need(baseline and fingerprints(batch) == baseline, "backup fault changed original five files")
    ledger = catalog.backup_coverage(bid)
    need(ledger and all(item["status"] == "FAILED" for item in ledger), "backup ledger did not retain FAILED")
    need(not list(destination.rglob("BACKUP_COMPLETE.json")), "failed backup has completion marker")
    audit = inspect_actual_fifteen(catalog, bid, bundle)
    evidence = {"status": "PASS", "case": "backup_device_unavailable_after_completed_generation",
                "fault_injection_kind": "in-process QA syscall exception", "physical_device_disconnected": False,
                "data_root": str(data), "destination": str(destination), "injected_calls": injected,
                "actual_generation_result": result, "actual_catalog_state": row, "actual_failed_ledger": ledger,
                "source_five_files_before_backup": baseline, "source_five_files_after_failure": fingerprints(batch),
                "actual_fifteen": audit, "batch_count": catalog.count(), "backup_complete_marker_count": 0}
    save(run / "backup-failure-result.json", evidence)
    return evidence, catalog, bid


def export_failure_case(run, catalog, bid):
    from number_app.amount_mode import export as module
    destination = run / "failed-export"
    original_copy = module.shutil.copyfile
    baseline, original_row = fingerprints(catalog.path(bid)), catalog.get(bid)
    calls = []
    def copy_fault(source, target, *args, **kwargs):
        if Path(target).resolve(strict=False).is_relative_to(destination.resolve(strict=False)):
            calls.append({"source": str(source), "target": str(target)})
            if len(calls) == 3:
                raise PermissionError(errno.EACCES, "QA-only export write permission failure after two real copies")
        return original_copy(source, target, *args, **kwargs)
    caught = None
    module.shutil.copyfile = copy_fault
    try:
        module.export_amount_batch(catalog.root, bid, destination)
    except PermissionError as error:
        caught = {"type": type(error).__name__, "errno": error.errno, "message": str(error)}
    finally:
        module.shutil.copyfile = original_copy
    partial = list(destination.glob("." + bid + ".*.partial"))
    need(caught is not None and len(calls) == 3 and len(partial) == 1, "actual export did not encounter intended mid-copy failure")
    files = [path for path in partial[0].iterdir() if path.is_file()]
    need(len(files) == 2 and not (destination / bid).exists(), "partial export was published or actual copied files missing")
    need(fingerprints(catalog.path(bid)) == baseline and catalog.get(bid) == original_row and catalog.count() == 1,
         "failed export changed/re-generated original batch or catalog state")
    result = {"status": "PASS", "case": "mid_export_permission_failure", "fault_injection_kind": "in-process QA syscall exception",
              "actual_exception": caught, "actual_copy_calls": calls, "unpublished_partial_directory": str(partial[0]),
              "actually_copied_files": {path.name: sha(path) for path in files}, "formal_export_exists": False,
              "source_five_files_preserved": baseline, "catalog_state_unchanged": True, "batch_reused": bid}
    save(run / "export-failure-result.json", result)
    return result


def run_frozen_io(run_directory):
    run = Path(run_directory)
    summary = {"status": "FAIL", "pid": os.getpid(), "fault_injection_kind": "in-process QA syscall exception",
               "not_a_source_model": True, "physical_disk_or_device_modified_to_fail": False, "cases": [], "errors": []}
    try:
        bundle, origins = setup_bundle()
        summary["runtime"] = {"executable": sys.executable, "frozen": True, "bundle_root": str(bundle), "module_origins": origins,
                              "qa_target": __file__}
        summary["cases"].append(disk_full_case(run))
        backup, catalog, bid = backup_failure_case(run, bundle)
        summary["cases"].append(backup)
        summary["cases"].append(export_failure_case(run, catalog, bid))
        summary["status"] = "PASS"
    except BaseException as error:
        summary["errors"].append(type(error).__name__ + ": " + str(error))
        summary["traceback"] = traceback.format_exc()
    save(run / "io-result.json", summary)
    if summary["status"] != "PASS": raise SystemExit(3)
