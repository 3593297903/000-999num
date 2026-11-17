import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from number_app.amount_mode import backup as backup_module
from number_app.amount_mode import audit as audit_module
from number_app.amount_mode import restore as restore_module
from number_app.amount_mode.backup import backup_amount_batch, snapshot_amount_catalog, validate_amount_backup
from number_app.amount_mode.catalog import Catalog
from number_app.amount_mode.protocol import FIXED_FILES, canonical_json, digest, strict_json
from number_app.amount_mode.recovery import verify_amount_batch
from number_app.amount_mode.restore import restore_amount_backup, restore_amount_backups
from number_app.amount_mode.service import generate_amount_batch

RUN = Path(os.environ.get("AMOUNT_QA_ROOT", "E:/number/qa/amount_mode/r17-ddb2bc"))


def _tree(root):
    result = {}
    def visit(path):
        if path.is_symlink() or path.is_junction():
            result[path.relative_to(root).as_posix()] = ("link", str(path.resolve()))
        elif path.is_dir():
            result[path.relative_to(root).as_posix()] = ("dir",)
            for child in path.iterdir():
                visit(child)
        else:
            result[path.relative_to(root).as_posix()] = ("file", path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
    visit(Path(root))
    return result


def _crashed_checker(*args):
    os._exit(7)


def _reseal_outer_qa_only(package):
    """Test-only corruption fixture; never part of restore or the application."""
    manifest_path = package / "backup_manifest.json"
    manifest = strict_json(manifest_path.read_bytes())
    for entry in manifest["files"]:
        value = (package / entry["path"]).read_bytes()
        entry.update(size=len(value), sha256=digest(value))
    data = canonical_json(manifest)
    manifest_path.write_bytes(data)
    marker_path = package / "BACKUP_COMPLETE.json"
    marker = strict_json(marker_path.read_bytes())
    marker["backup_manifest_sha256"] = digest(data)
    marker_path.write_bytes(canonical_json(marker))


@pytest.fixture(scope="module")
def originals():
    base = RUN / "backup-native" / uuid.uuid4().hex[:8]
    source, backups = base / "data", base / "backups"
    results = [generate_amount_batch(amounts, source, backup_root=backups) for amounts in ((1, 5, 2), (5, 5))]
    assert all(result["backup"]["status"] == "COMPLETE" for result in results), results
    return base, source, backups, results


def test_same_day_each_batch_has_own_self_contained_original_anchor_package(originals):
    base, source, backups, results = originals
    assert results[0]["batch_id"] != results[1]["batch_id"]
    package_sizes = []
    for result in results:
        package = Path(result["backup"]["package_path"])
        valid = validate_amount_backup(package)
        assert valid["batch_id"] == result["batch_id"]
        assert valid["covered_audit_ids"] == [result["audit_id"]]
        assert valid["context"].expected_request_sha256 == result["request_sha256"]
        assert valid["context"].expected_manifest_sha256 == result["snapshot_digest"]
        assert valid["record"]["batch_relpath"] == "batches/" + result["batch_id"]
        assert str(source) not in (package / "catalog_record.json").read_text(encoding="utf-8")
        for name in FIXED_FILES:
            assert (package / "batch" / name).read_bytes() == (Path(result["batch_dir"]) / name).read_bytes()
        assert len(list((package / "batch/audits" / result["audit_id"]).glob("[ABC]_*.json"))) == 15
        assert not list(package.rglob("history.sqlite"))
        package_sizes.append(sum(p.stat().st_size for p in package.rglob("*") if p.is_file()))
    (base / "per_batch_coverage.json").write_bytes(canonical_json({"results": results, "package_sizes": package_sizes}))


def test_reverify_core_reuse_keeps_coverage_and_clears_failed_backup_status(originals, tmp_path, monkeypatch):
    base, source, backups, results = originals
    first = results[0]
    package = Path(first["backup"]["package_path"])
    before = _tree(package)
    verified = verify_amount_batch(first["batch_dir"], source)
    assert verified["checks_passed"] == 15 and verified["audit_id"] != first["audit_id"]
    copied = backup_module._copy_verified
    def full(*args, **kwargs):
        raise OSError("QA injected no space on backup volume")
    monkeypatch.setattr(backup_module, "_copy_verified", full)
    with pytest.raises(OSError, match="no space"):
        backup_amount_batch(source, first["batch_id"], tmp_path / "other-volume")
    row = Catalog(source).get(first["batch_id"])
    assert row["generation_status"] == "COMPLETED" and row["backup_status"] == "FAILED"
    monkeypatch.setattr(backup_module, "_copy_verified", copied)
    reused = backup_amount_batch(source, first["batch_id"], backups)
    assert reused["status"] == "REUSED" and reused["package_path"] == str(package)
    assert reused["covered_audit_ids"] == [first["audit_id"]]
    assert reused["latest_audit_covered"] is False
    assert verified["audit_id"] not in reused["covered_audit_ids"]
    assert Catalog(source).get(first["batch_id"])["backup_status"] == "COMPLETE"
    assert _tree(package) == before


def test_restore_with_original_root_unavailable_checks_target_and_rejects_bad_package(originals, tmp_path, monkeypatch):
    base, source, backups, results = originals
    result = results[0]
    package = Path(result["backup"]["package_path"])
    hidden = source.with_name("original-offline")
    assert source.resolve().is_relative_to(RUN.resolve()) and hidden.resolve().is_relative_to(RUN.resolve())
    source.rename(hidden)
    read_targets = []
    actual_audit = restore_module.audit_amount_batch
    target = tmp_path / "restored"
    def target_only(batch, context, **kwargs):
        read_targets.append(str(batch))
        assert Path(batch).resolve().is_relative_to(target.resolve())
        return actual_audit(batch, context, **kwargs)
    monkeypatch.setattr(restore_module, "audit_amount_batch", target_only)
    try:
        restored = restore_amount_backup(package, target)
        assert restored["status"] == "COMPLETED" and restored["checks_passed"] == 15
        assert restored["audit_id"] not in result["backup"]["covered_audit_ids"]
        assert restored["verified_target_root"] == str(target.resolve()) and read_targets
        catalog = Catalog(target)
        assert catalog.get(result["batch_id"])["batch_relpath"] == "batches/" + result["batch_id"]
        assert catalog.context(result["batch_id"]).expected_manifest_sha256 == result["snapshot_digest"]
        for name in FIXED_FILES:
            assert (Path(restored["batch_dir"]) / name).read_bytes() == (package / "batch" / name).read_bytes()
        (tmp_path / "independent_restore.json").write_bytes(canonical_json({"original_root_unavailable": True,
            "source_was": str(source), "read_targets": read_targets, "result": restored}))
    finally:
        hidden.rename(source)
    bad = tmp_path / "bad-package"
    shutil.copytree(package, bad)
    p = bad / "batch/groups.txt"
    p.write_bytes(p.read_bytes() + b"\n")
    bad_target = tmp_path / "not-created"
    with pytest.raises(ValueError):
        restore_amount_backup(bad, bad_target)
    assert not bad_target.exists()
    # Self-consistent outer hashes must not hide an unsafe source-directory
    # fallback in the catalogue metadata.
    unsafe = tmp_path / "unsafe-index-package"
    shutil.copytree(package, unsafe)
    record_path = unsafe / "catalog_record.json"
    record = strict_json(record_path.read_bytes())
    record["batch_relpath"] = str(source / "batches" / result["batch_id"])
    record_path.write_bytes(canonical_json(record))
    _reseal_outer_qa_only(unsafe)
    with pytest.raises(ValueError, match="绝对路径"):
        restore_amount_backup(unsafe, tmp_path / "unsafe-not-created")
    assert not (tmp_path / "unsafe-not-created").exists()
    future = tmp_path / "future-package"
    shutil.copytree(package, future)
    marker_path = future / "BACKUP_COMPLETE.json"
    marker = strict_json(marker_path.read_bytes())
    marker["backup_version"] = 2
    marker_path.write_bytes(canonical_json(marker))
    with pytest.raises(ValueError, match="备份版本"):
        restore_amount_backup(future, tmp_path / "future-not-created")
    assert not (tmp_path / "future-not-created").exists()


def test_partial_restore_never_uses_source_files_to_claim_complete(originals, tmp_path, monkeypatch):
    _, _, _, results = originals
    package = Path(results[1]["backup"]["package_path"])
    before = _tree(package)
    actual_copy = restore_module._copy_verified
    counter = []
    def partial(source, target, sha, size):
        counter.append(str(target))
        if len(counter) == 2:
            raise OSError("QA injected partial restore copy")
        return actual_copy(source, target, sha, size)
    monkeypatch.setattr(restore_module, "_copy_verified", partial)
    target = tmp_path / "partial"
    with pytest.raises(OSError, match="partial restore"):
        restore_amount_backup(package, target)
    row = Catalog(target).get(results[1]["batch_id"])
    assert row["generation_status"] == "FAILED" and row["manifest_sha256"] is None
    assert row["latest_verification_status"] != "PASS"
    assert not (target / "batches" / results[1]["batch_id"]).exists()
    assert _tree(package) == before


def test_backup_keeps_committed_failure_evidence_and_excludes_orphans(originals, tmp_path, monkeypatch):
    _, source, _, results = originals
    first = results[0]
    worker = audit_module._audit_worker
    monkeypatch.setattr(audit_module, "_audit_worker", _crashed_checker)
    with pytest.raises(ValueError, match="复核"):
        verify_amount_batch(first["batch_dir"], source)
    failed = Catalog(source).list_audits(first["batch_id"])[-1]
    assert failed["status"] == "FAIL"
    monkeypatch.setattr(audit_module, "_audit_worker", worker)
    new = verify_amount_batch(first["batch_dir"], source)
    assert new["checks_passed"] == 15
    orphan_id = uuid.uuid4().hex
    orphan = Path(first["batch_dir"]) / "audits" / orphan_id
    orphan.mkdir()
    (orphan / "partial.txt").write_bytes(b"not a committed audit")
    saved = backup_amount_batch(source, first["batch_id"], tmp_path / "archive")
    valid = validate_amount_backup(saved["package_path"])
    records = {record["audit_id"]: record for record, _ in valid["audit_records"]}
    assert records[failed["audit_id"]]["status"] == "FAIL"
    assert failed["audit_id"] in saved["covered_audit_ids"]
    assert new["audit_id"] in saved["covered_audit_ids"]
    assert orphan_id not in saved["covered_audit_ids"]
    assert not (Path(saved["package_path"]) / "batch/audits" / orphan_id).exists()
    assert (Path(saved["package_path"]) / "batch/audits" / failed["audit_id"] / "summary.json").is_file()


def test_two_verified_index_slots_rotate_only_owned_derived_files(originals, tmp_path, monkeypatch):
    _, source, backups, results = originals
    batch_before = {result["batch_id"]: _tree(Path(result["batch_dir"])) for result in results}
    package_before = {result["batch_id"]: _tree(Path(result["backup"]["package_path"])) for result in results}
    first = snapshot_amount_catalog(source, backups)
    assert first["status"] == "SNAPSHOT_COMPLETE" and first["slot"] == "A"
    first_path = Path(first["snapshot_path"])
    first_bytes = first_path.read_bytes()
    second = snapshot_amount_catalog(source, backups)
    assert second["slot"] == "B" and first_path.read_bytes() == first_bytes
    assert set(second["slots"]) == {"A", "B"}
    sentinel = backups / "catalog/snapshots/user-keep.sqlite"
    sentinel.write_bytes(b"not a program-derived index")
    third = snapshot_amount_catalog(source, backups)
    assert set(third["slots"]) == {"A", "B"}
    assert sentinel.read_bytes() == b"not a program-derived index"
    assert len(list((backups / "catalog/snapshots").glob("amt-index-*.sqlite"))) == 2
    slots = backups / "catalog/slots"
    previous = {name: (slots / (name + ".json")).read_bytes() for name in "AB"}
    check = backup_module._snapshot_info
    def broken_new(file):
        if str(file).endswith(".partial"):
            raise OSError("QA injected snapshot verification failure")
        return check(file)
    monkeypatch.setattr(backup_module, "_snapshot_info", broken_new)
    with pytest.raises(OSError, match="snapshot verification"):
        snapshot_amount_catalog(source, backups)
    assert previous == {name: (slots / (name + ".json")).read_bytes() for name in "AB"}
    assert batch_before == {result["batch_id"]: _tree(Path(result["batch_dir"])) for result in results}
    assert package_before == {result["batch_id"]: _tree(Path(result["backup"]["package_path"])) for result in results}


def test_backup_rejects_batch_target_before_any_write_and_bulk_restores_each_new_fifteen(originals, tmp_path):
    _, source, backups, results = originals
    before = _tree(source)
    for destination in (Path(results[0]["batch_dir"]), Path(results[1]["batch_dir"]) / "never-created/deeper"):
        with pytest.raises(ValueError):
            backup_amount_batch(source, results[0]["batch_id"], destination)
        assert _tree(source) == before
    packages = [result["backup"]["package_path"] for result in results]
    restored = restore_amount_backups(packages, tmp_path / "bulk")
    assert restored["status"] == "COMPLETED" and restored["completed"] == 2 and restored["failed"] == 0
    assert all(result["status"] == "COMPLETED" and result["checks_passed"] == 15 for result in restored["batches"])
    assert all(result["audit_id"] not in {r["audit_id"] for r in results} for result in restored["batches"])
    (tmp_path / "bulk_restore.json").write_bytes(canonical_json(restored))


def test_bulk_cancel_stops_without_registering_next_package(originals, tmp_path):
    _, source, _, results = originals
    packages = [r['backup']['package_path'] for r in results]
    cancelled = [False]
    def progress(event):
        if event.get('phase') == 'audit_started': cancelled[0] = True
    target = tmp_path / 'cancel-bulk'
    before = _tree(source)
    result = restore_amount_backups(packages, target, cancel=lambda: cancelled[0], progress=progress)
    assert result['status'] == 'CANCELLED' and result['completed'] == 0 and result['cancelled'] == 2
    catalog = Catalog(target)
    assert catalog.count() == 1
    assert catalog.get(results[0]['batch_id'])['generation_status'] == 'CANCELLED'
    assert catalog.get(results[1]['batch_id']) is None
    assert _tree(source) == before
    never_started = tmp_path / 'cancel-before-any-write'
    result = restore_amount_backups(packages, never_started, cancel=lambda: True)
    assert result['status'] == 'CANCELLED' and not never_started.exists()
