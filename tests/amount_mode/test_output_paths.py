"""Public copy interfaces must reject protected paths before changing anything.

All batches, the old-style catalog and links are created inside this QA run.
No user batch or prior QA evidence is used as a fault-injection target.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import uuid

import pytest

from number_app.amount_mode import output_paths
from number_app.amount_mode.catalog import Catalog
from number_app.amount_mode.input_config import build_request_bytes
from number_app.amount_mode.protocol import FIXED_FILES
from number_app.amount_mode.recovery import verify_amount_batch
from number_app.amount_mode.service import generate_amount_batch
from number_app.amount_mode.spec import AmountBatchSpec


RUN = Path(os.environ.get("AMOUNT_QA_ROOT", "E:/number/qa/amount_mode/r17-ddb2bc"))


def _sha(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _tree(root):
    """Inventory every physical entry; record links without walking their targets."""
    result = {".": {"kind": "directory"}}
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or path.is_junction():
                result[relative] = {"kind": "junction" if path.is_junction() else "symlink",
                                    "resolved": str(path.resolve(strict=False))}
            elif path.is_dir():
                result[relative] = {"kind": "directory"}
                pending.append(path)
            else:
                result[relative] = {"kind": "file", "size": path.stat().st_size, "sha256": _sha(path)}
    return result


def _database_state(database):
    # URI mode=ro avoids creating a database while observing the preflight.
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        tables = {}
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            quoted = '"' + name.replace('"', '""') + '"'
            rows = connection.execute("SELECT * FROM " + quoted).fetchall()
            tables[name] = sorted(rows, key=lambda row: json.dumps(row, ensure_ascii=False))
        return {"application_id": connection.execute("PRAGMA application_id").fetchone()[0],
                "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
                "schema": schema, "tables": tables}
    finally:
        connection.close()


def _snapshot(state):
    return {"tree": _tree(state["base"]),
            "databases": {str(path.relative_to(state["base"])): _database_state(path)
                          for path in state["databases"]}}


def _write_evidence(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")


@pytest.fixture(scope="module")
def protected_workspace():
    base = RUN / "pathguard" / uuid.uuid4().hex[:8]
    base.mkdir(parents=True)
    old_root, data_root = base / "old-root", base / "data"
    old_root.mkdir()
    old_registered = {}
    connection = sqlite3.connect(old_root / "history.sqlite")
    try:
        connection.execute("CREATE TABLE batches(batch_id TEXT PRIMARY KEY,batch_dir TEXT,status TEXT)")
        for status in ("COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"):
            path = base / "old-registered" / status.lower()
            path.mkdir(parents=True)
            (path / "original-sentinel.txt").write_bytes(("preserve " + status + "\n").encode())
            connection.execute("INSERT INTO batches VALUES(?,?,?)", ("old-" + status.lower(), str(path), status))
            old_registered[status] = path
        connection.commit()
    finally:
        connection.close()
    patch = pytest.MonkeyPatch()
    patch.setattr(output_paths, "DEFAULT_ROOTS", (old_root, data_root))
    try:
        batches = [generate_amount_batch(amounts, data_root, backup_root=base / "automatic-backups")
                   for amounts in ((1,), (1, 5, 2))]
        assert all(item["status"] == "COMPLETED" and item["checks_passed"] == 15 for item in batches)
        catalog = Catalog(data_root)
        failed_bid = uuid.uuid4().hex
        spec = AmountBatchSpec((1,))
        catalog.create(spec, build_request_bytes(spec, batch_id=failed_bid))
        catalog.transition(failed_bid, "FAILED", error="QA-only failed registration")
        failed_path = catalog.path(failed_bid)
        failed_path.mkdir(parents=True)
        (failed_path / "failure-evidence.txt").write_bytes(b"must remain unchanged\n")
        external = base / "external-marked-batch"
        external.mkdir()
        # Marker recognition must still protect an externally damaged batch.
        (external / "request.json").write_bytes(b"not valid JSON\n")
        (external / "manifest.json").write_bytes(b"damaged manifest\n")
        (external / "original-evidence.txt").write_bytes(b"do not overwrite\n")
        existing_file = base / "file-cannot-be-a-directory"
        existing_file.write_bytes(b"original file\n")
        batch_b = Path(batches[1]["batch_dir"])
        alias = base / "junction-to-B"
        resolved_target = batch_b.resolve(strict=True)
        assert resolved_target.is_relative_to(RUN.resolve(strict=True))
        assert alias.resolve(strict=False).is_relative_to(RUN.resolve(strict=True))
        assert not alias.exists()
        command = ["cmd.exe", "/c", "mklink", "/J", str(alias), str(resolved_target)]
        created = subprocess.run(command, capture_output=True, timeout=15)
        _write_evidence(base / "junction-creation.json", {
            "command": command, "exit_code": created.returncode,
            "stdout": created.stdout.decode(errors="replace"), "stderr": created.stderr.decode(errors="replace"),
            "checked_absolute_target": str(resolved_target), "qa_root": str(RUN.resolve(strict=True))})
        assert created.returncode == 0, "Real QA junction creation failed; this test is not skipped"
        assert alias.is_junction() and alias.resolve(strict=True) == resolved_target
        state = {"base": base, "root": data_root, "old_root": old_root,
                 "old_registered": old_registered, "batches": batches, "failed_path": failed_path,
                 "external": external, "alias": alias, "existing_file": existing_file,
                 "databases": (data_root / "history.sqlite", old_root / "history.sqlite"), "proofs": []}
        _write_evidence(base / "initial-generation.json", batches)
        assert (data_root / ".coordinator.lock").is_file()
        yield state
    finally:
        # Preserve all batches, reports, packages, failed fixtures and the junction.
        patch.undo()


def _reject_without_changes(state, label, operation):
    before = _snapshot(state)
    error, returned = None, None
    try:
        returned = operation()
    except Exception as caught:
        error = caught
    after = _snapshot(state)
    proof = {"case": label, "exception_type": type(error).__name__ if error is not None else None,
             "exception": str(error) if error is not None else None,
             "returned": returned, "before": before, "after": after,
             "complete_tree_file_hashes_and_database_state_unchanged": before == after}
    state["proofs"].append(proof)
    if not isinstance(error, ValueError) or before != after:
        _write_evidence(state["base"] / ("FAIL-" + uuid.uuid4().hex[:8] + ".json"), proof)
    assert isinstance(error, ValueError), f"{label}: must reject protected destination before writing; got {error!r}, returned {returned!r}"
    assert before == after, f"{label}: rejected operation changed a directory, file fingerprint or database state"


def test_public_backup_export_preflight_and_followup_fifteen(protected_workspace):
    from number_app.amount_mode.backup import backup_amount_batch
    from number_app.amount_mode.export import export_amount_batch

    state = protected_workspace
    root, first, second = state["root"], *state["batches"]
    source, other = Path(first["batch_dir"]), Path(second["batch_dir"])
    destinations = {
        "source-batch": source,
        "source-nonexistent-descendant": source / "absent" / "deeper",
        "other-batch": other,
        "A-into-B-absent-descendant": other / "absent" / "deeper",
        "new-batches-reserved-absent": root / "batches" / "unregistered" / "deeper",
        "new-staging-reserved-absent": root / "staging" / "unregistered" / "deeper",
        "failed-new-batch": state["failed_path"] / "absent",
        "external-damaged-markers": state["external"] / "absent" / "deeper",
        "case-variant-other-batch": Path(str(other).swapcase()) / "absent",
        "real-junction-other-batch": state["alias"] / "absent" / "deeper",
        "old-batches-reserved": state["old_root"] / "batches" / "unregistered",
        "old-staging-reserved": state["old_root"] / "staging" / "unregistered",
        "file-as-directory": state["existing_file"] / "absent",
    }
    destinations.update({"old-registered-" + status: path / "absent" / "deeper"
                         for status, path in state["old_registered"].items()})
    operations = {"export": export_amount_batch, "backup": backup_amount_batch}
    for name, operation in operations.items():
        for label, destination in destinations.items():
            _reject_without_changes(state, name + "/" + label,
                                    lambda op=operation, dest=destination: op(root, first["batch_id"], dest))

    # These direct guards exercise the other external write-path contracts with
    # their actual file-vs-directory semantics, without making a target first.
    direct = {
        "result-json": (other / "uncreated-results" / "receipt.json", True),
        "data-directory": (source / "uncreated-data", False),
        "log-file": (other / "uncreated-logs" / "app.log", True),
        "draft-file": (other / "uncreated-drafts" / "input.json", True),
        "cache-file": (other / "uncreated-cache" / "index.json", True),
        "temporary-copy-directory": (source / ".export-not-created", False),
    }
    for label, (target, is_file) in direct.items():
        _reject_without_changes(state, "direct/" + label,
                                lambda p=target, f=is_file: output_paths.protect_paths(
                                    p, workspace_roots=(root,), file_paths=(p,) if f else ()))

    # Resolve errors must fail before touching a lock, target, catalog or ledger.
    impossible = state["base"] / "resolver-fault-never-create"
    real_resolve = Path.resolve
    def resolver_fault(path, *args, **kwargs):
        if path == impossible:
            raise OSError("QA injected path resolution error")
        return real_resolve(path, *args, **kwargs)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "resolve", resolver_fault)
        for name, operation in operations.items():
            _reject_without_changes(state, name + "/resolve-error",
                                    lambda op=operation: op(root, first["batch_id"], impossible))

    original = {item["batch_id"]: {name: _sha(Path(item["batch_dir"]) / name) for name in FIXED_FILES}
                for item in state["batches"]}
    successes = []
    for item in state["batches"]:
        exported = export_amount_batch(root, item["batch_id"], state["base"] / "normal-exports")
        assert exported["status"] == "EXPORTED"
        exported_path = Path(exported["path"])
        assert exported_path == (state["base"] / "normal-exports" / item["batch_id"]).resolve()
        assert {name: _sha(exported_path / name) for name in FIXED_FILES} == original[item["batch_id"]]
        backup = backup_amount_batch(root, item["batch_id"], state["base"] / "normal-backups")
        assert backup["status"] == "COMPLETE"
        package = Path(backup["package_path"])
        assert package.is_relative_to((state["base"] / "normal-backups").resolve())
        assert (package / "BACKUP_COMPLETE.json").is_file()
        assert {name: _sha(package / "batch" / name) for name in FIXED_FILES} == original[item["batch_id"]]
        assert item["audit_id"] in backup["covered_audit_ids"]
        successes.append({"batch_id": item["batch_id"], "export": exported, "backup": backup})
    verified = []
    for item in state["batches"]:
        result = verify_amount_batch(item["batch_dir"], root)
        assert result["status"] == "PASS" and result["checks_passed"] == 15
        assert result["audit_id"] != item["audit_id"]
        assert {name: _sha(Path(item["batch_dir"]) / name) for name in FIXED_FILES} == original[item["batch_id"]]
        verified.append(result)
    _write_evidence(state["base"] / "pathguard-evidence.json", {
        "rejections": state["proofs"], "normal_operations": successes,
        "followup_real_fifteen_each": verified,
        "fixed_hashes_before_and_after": original,
        "note": "Full physical directory trees and every file SHA, plus all SQLite tables, compared for every refusal. Junction retained; no old data used."})
