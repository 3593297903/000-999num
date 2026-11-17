"""QA-only packaged acceptance support, without importing application modules."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from datetime import datetime

import psutil

PROJECT = Path(__file__).resolve().parents[2]
MODE = "set_amount_small_v1"
FIXED = ("request.json", "group_plan.json", "items.jsonl", "groups.txt", "manifest.json")
PROOFS = ("request_matches_frozen_input", "snapshot_matches_trusted_hashes", "source_code_cells_exact",
          "raw_per_code_exact", "per_record_amount_exact", "group_sizes_exact", "group_code_amounts_exact",
          "final_per_code_amount_exact", "final_total_amount_exact", "final_format_canonical")


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(canonical(value))


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def read_json(path):
    def bad_constant(value):
        raise ValueError("Nonstandard JSON number: " + value)
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_pairs, parse_constant=bad_constant)


def fixed_hashes(batch):
    return {name: sha(Path(batch) / name) for name in FIXED}


def tree_fingerprint(root):
    """Complete local tree and file bytes, never follow a junction or symlink."""
    root = Path(root)
    if not root.exists():
        return {".": {"kind": "missing"}}
    result = {".": {"kind": "directory"}}
    for folder, directories, files in os.walk(root, followlinks=False):
        parent = Path(folder)
        kept = []
        for name in directories:
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or path.is_junction():
                result[relative] = {"kind": "link", "target": str(path.resolve())}
            else:
                result[relative] = {"kind": "directory"}
                kept.append(name)
        directories[:] = kept
        for name in files:
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[relative] = {"kind": "link", "target": str(path.resolve())}
            else:
                stat = path.stat()
                result[relative] = {"kind": "file", "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": sha(path)}
    return result


def assert_audit(batch, result, amounts, dist, executable):
    """Independent acceptance gate: actual raw files, every field and process instance."""
    batch, dist, executable = Path(batch), Path(dist).resolve(), Path(executable).resolve()
    aid = result["audit_id"]
    session = batch / "audits" / aid
    expected_names = {f"{m}_{r}.json" for m in "ABC" for r in range(1, 6)}
    assert {p.name for p in session.iterdir()} == expected_names | {"summary.json", "summary.txt"}
    assert (session / "summary.txt").is_file() and (session / "summary.txt").stat().st_size > 0
    summary = read_json(session / "summary.json")
    assert canonical(summary) == (session / "summary.json").read_bytes()
    expected = {"set_count": len(amounts), "raw_total": len(amounts) * 1000,
                "group_count": result.get("group_count", summary["expected"]["group_count"]),
                "per_code_amount": str(sum(amounts)), "total_amount": str(1000 * sum(amounts))}
    for key, value in expected.items():
        assert type(summary["expected"][key]) is type(value) and summary["expected"][key] == value
    assert summary["status"] == "PASS" and summary["checks_passed"] == 15 and summary["errors"] == []
    assert summary["batch_id"] == result["batch_id"] and summary["audit_id"] == aid
    assert summary["expected_request_sha256"] == result["request_sha256"]
    assert summary["expected_manifest_sha256"] == result["snapshot_digest"]
    assert sha(batch / "request.json") == result["request_sha256"] and sha(batch / "manifest.json") == result["snapshot_digest"]
    checks = summary["checks"]
    assert type(checks) is list and len(checks) == 15
    pairs, instances, execution_ids, reports = set(), set(), set(), []
    actual_statistics = {"actual_raw_total": expected["raw_total"], "actual_group_count": expected["group_count"],
                         "actual_total_amount": expected["total_amount"], "checked_codes": 1000,
                         "checked_source_code_cells": expected["raw_total"], "files_read": 5}
    for execution in checks:
        method, round_no = execution["method"], execution["round"]
        assert method in "ABC" and len(method) == 1 and type(round_no) is int and 1 <= round_no <= 5
        assert (method, round_no) not in pairs
        pairs.add((method, round_no))
        assert type(execution["exit_code"]) is int and execution["exit_code"] == 0
        identity = execution["pid"], execution["process_created_filetime"]
        assert all(type(v) is int and v > 0 for v in identity) and identity not in instances
        instances.add(identity)
        assert execution["execution_id"] not in execution_ids
        execution_ids.add(execution["execution_id"])
        name = f"{method}_{round_no}.json"
        assert execution["report_path"] == name
        path = session / name
        assert sha(path) == execution["report_sha256"]
        report = read_json(path)
        assert canonical(report) == path.read_bytes()
        for key, value in {"mode": MODE, "batch_id": result["batch_id"], "audit_id": aid,
                           "method": method, "round": round_no, "execution_id": execution["execution_id"],
                           "expected_request_sha256": result["request_sha256"],
                           "expected_manifest_sha256": result["snapshot_digest"]}.items():
            assert type(report[key]) is type(value) and report[key] == value
        assert report["pid"] == identity[0] and report["process_created_filetime"] == identity[1]
        assert report["status"] == "PASS" and report["errors"] == []
        for key, value in actual_statistics.items():
            assert type(report[key]) is type(value) and report[key] == value
        assert report["expected"] == expected
        extras = ("standard_records_equal",) if method == "B" else (("unique_ids_exact", "output_bytes_equal") if method == "C" else ())
        assert all(report["proofs"][name] is True for name in (*PROOFS, *extras))
        runtime = report["runtime"]
        assert runtime["frozen"] is True
        assert Path(runtime["executable"]).resolve() == executable
        bundle = Path(runtime["bundle_root"]).resolve()
        assert bundle.is_relative_to(dist)
        assert Path(runtime.get("checker_module_file", runtime["checker_module"])).resolve().is_relative_to(bundle)
        reports.append(str(path))
    assert pairs == {(m, r) for m in "ABC" for r in range(1, 6)}
    return {"audit_id": aid, "summary_path": str(session / "summary.json"), "summary_sha256": sha(session / "summary.json"),
            "report_paths": reports, "new_process_instances": len(instances), "expected": expected}


class ReleaseHarness:
    def __init__(self, dist_dir, qa_root):
        self.dist = Path(dist_dir).resolve(strict=True)
        self.tool = self.dist / "AmountTool.exe"
        self.verify = self.dist / "AmountVerify.exe"
        assert self.tool.is_file() and self.verify.is_file(), "Both actual candidate executables are required"
        self.exe_sha256 = self.current_hashes()
        self.root = Path(qa_root) / ("exe-" + uuid.uuid4().hex[:6])
        self.root.mkdir(parents=True)
        self.data = self.root / "d"
        self.backups = self.root / "bk"
        self.ledger = self.root / "release-ledger.jsonl"
        self.counter = 0
        self.batches = {}
        self.fixture = read_json(PROJECT / "按套金额随机小组_验收样例.json")
        self.manifest = {"evidence_version": 1, "created_at": datetime.now().astimezone().isoformat(),
                         "dist_dir": str(self.dist), "exe_sha256": self.exe_sha256,
                         "qa_root": str(self.root), "dependency_lock_sha256": sha(PROJECT / "requirements-dev.lock"),
                         "fixture_sha256": sha(PROJECT / "按套金额随机小组_验收样例.json"),
                         "offline_reader_sha256": sha(PROJECT / "scripts/amount_offline.py"),
                         "test_source_sha256": {name: sha(Path(__file__).with_name(name))
                                                for name in ("support_release.py", "test_release.py")}}
        write_json(self.root / "candidate.json", self.manifest)

    def current_hashes(self):
        return {path.name: sha(path) for path in (self.tool, self.verify)}

    def _append(self, value):
        with self.ledger.open("ab") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())

    def invoke(self, command, *args, executable=None, expected_exit=0, receipt_path=None,
               require_receipt=True, timeout=1200):
        self.counter += 1
        directory = self.root / "cmd" / f"{self.counter:03d}-{command}"
        directory.mkdir(parents=True)
        receipt = Path(receipt_path) if receipt_path is not None else directory / "result.json"
        executable = Path(executable or self.tool)
        values = [str(executable), command, *map(str, args), "--result-json", str(receipt)]
        before_hashes = self.current_hashes()
        assert before_hashes == self.exe_sha256
        evidence = {"command": values, "started_at": datetime.now().astimezone().isoformat(),
                    "exe_sha256": before_hashes, "result_json": str(receipt), "expected_exit_code": expected_exit,
                    "exit_code": None, "status": "RUNNING"}
        write_json(directory / "command.json", evidence)
        started = time.monotonic()
        peak_rss = peak_handles = peak_children = 0
        descendants = {}
        sample_errors = []
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["PYTHONIOENCODING"] = "utf-8"
        with (directory / "stdout.log").open("wb") as output, (directory / "stderr.log").open("wb") as error:
            process = subprocess.Popen(values, cwd=self.dist, env=environment, stdout=output, stderr=error)
            evidence["pid"] = process.pid
            parent = psutil.Process(process.pid)
            timed_out = False
            while process.poll() is None:
                try:
                    children = parent.children(recursive=True)
                    current = [parent, *children]
                    rss = handles = 0
                    for instance in current:
                        try:
                            rss += instance.memory_info().rss
                            handles += instance.num_handles()
                            if instance.pid != parent.pid:
                                descendants[(instance.pid, instance.create_time())] = True
                        except psutil.NoSuchProcess:
                            pass
                    peak_rss, peak_handles, peak_children = max(peak_rss, rss), max(peak_handles, handles), max(peak_children, len(children))
                except psutil.NoSuchProcess:
                    pass
                except psutil.Error as issue:
                    sample_errors.append(str(issue))
                if time.monotonic() - started > timeout:
                    timed_out = True
                    for instance in children if "children" in locals() else []:
                        try: instance.kill()
                        except psutil.Error: pass
                    process.kill()
                    break
                time.sleep(0.05)
            code = process.wait(timeout=15)
        survivors = []
        deadline = time.monotonic() + 5
        while True:
            survivors = []
            for (pid, creation) in descendants:
                try:
                    instance = psutil.Process(pid)
                    if instance.create_time() == creation and instance.is_running():
                        survivors.append(pid)
                except psutil.NoSuchProcess:
                    pass
            if not survivors or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        after_hashes = self.current_hashes()
        evidence.update(status="TIMEOUT" if timed_out else "FINISHED", exit_code=code,
                        ended_at=datetime.now().astimezone().isoformat(), elapsed_seconds=time.monotonic() - started,
                        peak_tree_rss_bytes=peak_rss, peak_tree_handles=peak_handles, peak_children=peak_children,
                        idle_children=survivors, resource_sample_errors=sample_errors, exe_sha256_after=after_hashes,
                        stdout_sha256=sha(directory / "stdout.log"), stderr_sha256=sha(directory / "stderr.log"))
        write_json(directory / "command.json", evidence)
        assert not timed_out, f"Actual exe timed out: {directory}"
        assert code == expected_exit, f"Unexpected actual exe exit {code}, expected {expected_exit}: {directory}"
        assert before_hashes == after_hashes and not survivors and not sample_errors
        if require_receipt:
            assert receipt.is_file(), f"Actual exe receipt missing: {directory}"
        result = read_json(receipt) if receipt.is_file() else None
        return result, evidence, directory

    def generate(self, label, amounts):
        amounts = tuple(amounts)
        config = self.root / "in" / (label + ".json")
        write_json(config, {"set_count": len(amounts), "amounts": [str(a) for a in amounts]})
        result, command, directory = self.invoke("generate", "--config", config, "--data-dir", self.data, "--backup-dir", self.backups)
        assert result["status"] == "COMPLETED" and result["checks_passed"] == 15 and result["mode"] == MODE
        assert result["set_count"] == len(amounts) and result["raw_total"] == len(amounts) * 1000
        assert result["per_code_expected_amount"] == str(sum(amounts)) and result["total_amount"] == str(sum(amounts) * 1000)
        assert (len(amounts) * 1000 + 4) // 5 <= result["group_count"] <= len(amounts) * 500
        assert result["backup"]["status"] == "COMPLETE", result["backup"]
        assert Path(result["backup"]["package_path"], "BACKUP_COMPLETE.json").is_file()
        audit = assert_audit(result["batch_dir"], result, amounts, self.dist, self.tool)
        offline_path = directory / "offline.json"
        offline_command = [sys.executable, str(PROJECT / "scripts/amount_offline.py"), "--batch-dir", result["batch_dir"],
                           "--config", str(config), "--result-json", str(offline_path)]
        before = time.monotonic()
        completed = subprocess.run(offline_command, capture_output=True, timeout=300)
        write_json(directory / "offline-command.json", {"command": offline_command, "exit_code": completed.returncode,
                                                        "elapsed_seconds": time.monotonic() - before,
                                                        "stdout": completed.stdout.decode("utf-8", errors="replace"),
                                                        "stderr": completed.stderr.decode("utf-8", errors="replace")})
        assert completed.returncode == 0
        offline = read_json(offline_path)
        assert offline["status"] == "PASS" and offline["raw_total"] == len(amounts) * 1000
        assert offline["unique_ids_present"] == offline["checked_source_code_cells"] == len(amounts) * 1000
        assert len(offline["source_cell_counts"]) == len(amounts)
        assert all(len(row) == 1000 and all(v == 1 for v in row) for row in offline["source_cell_counts"])
        assert len(offline["per_code_statistics"]) == 1000
        assert all(row["actual_raw_count"] == len(amounts) and row["actual_amount"] == str(sum(amounts))
                   and row["raw_difference"] == 0 and row["amount_difference"] == "0" for row in offline["per_code_statistics"])
        assert len(offline["group_checks"]) == result["group_count"]
        assert all(2 <= row["raw_count"] <= 5 and row["map_equal"] is True and row["canonical_body_equal"] is True
                   for row in offline["group_checks"])
        record = {"evidence_version": 1, "kind": "generation", "status": "PASS", "label": label,
                  "dist_dir": str(self.dist), "exe_sha256": self.exe_sha256,
                  "amounts": [str(a) for a in amounts], "set_count": len(amounts),
                  "config_path": str(config), "config_sha256": sha(config), "receipt_path": command["result_json"],
                  "receipt_sha256": sha(command["result_json"]), "command_evidence": str(directory / "command.json"),
                  "batch_id": result["batch_id"], "batch_dir": result["batch_dir"], "audit_id": result["audit_id"],
                  "raw_total": result["raw_total"], "group_count": result["group_count"],
                  "per_code_expected_amount": result["per_code_expected_amount"], "total_amount": result["total_amount"],
                  "request_sha256": result["request_sha256"], "snapshot_digest": result["snapshot_digest"],
                  "checks_passed": 15, "audit_evidence": audit, "offline_path": str(offline_path),
                  "offline_sha256": sha(offline_path), "completed_at": datetime.now().astimezone().isoformat()}
        self._append(record)
        value = {"result": result, "amounts": amounts, "config": config, "record": record}
        self.batches[label] = value
        return value
