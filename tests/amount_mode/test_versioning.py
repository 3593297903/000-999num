import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

from number_app.amount_mode.catalog import Catalog
from number_app.amount_mode.protocol import canonical_json
from number_app.amount_mode.service import generate_amount_batch
from number_app.amount_mode.versioning import (
    LEGACY_FIXTURE_PROVENANCE, SUPPORT_TABLE, inspect_amount_catalog,
    migrate_amount_catalog, prevent_destructive_index_rollback,
)

RUN = Path(os.environ.get("AMOUNT_QA_ROOT", "E:/number/qa/amount_mode/r17-ddb2bc"))


def _fixed_and_reports(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (root / "batches").rglob("*") if p.is_file()}


@pytest.fixture(scope="module")
def compatibility_fixture():
    base = RUN / "version-native" / uuid.uuid4().hex[:8]
    published = base / "current"
    result = generate_amount_batch((100000000000000003, 100000000000000019), published,
                                   backup_root=base / "backups")
    assert result["status"] == "COMPLETED" and result["checks_passed"] == 15
    legacy = base / "unpublished-1000"
    legacy.mkdir()
    shutil.copytree(published / "batches", legacy / "batches")
    # A real SQLite backup followed by explicit schema changes, not a model or
    # a fake application upgrade. This index version was never released.
    destination = sqlite3.connect(legacy / "history.sqlite", isolation_level=None)
    try:
        with Catalog(published).connection() as source:
            source.backup(destination)
        destination.execute("ALTER TABLE batches DROP COLUMN backup_status")
        destination.execute("ALTER TABLE batches DROP COLUMN backup_error")
        destination.execute("PRAGMA user_version=1000")
    finally:
        destination.close()
    (base / "fixture-provenance.json").write_bytes(canonical_json({
        "provenance": LEGACY_FIXTURE_PROVENANCE, "real_previous_amount_tool_release": False,
        "created_from_current_candidate_qa_batch": result, "legacy_index": str(legacy / "history.sqlite")}))
    return base, published, legacy, result


def test_explicit_support_table_and_known_fixture_migration_preserves_exact_data(compatibility_fixture, tmp_path):
    _, _, legacy, batch = compatibility_fixture
    root = tmp_path / "migrate"
    shutil.copytree(legacy, root)
    fixed = _fixed_and_reports(root)
    before = inspect_amount_catalog(root)
    assert before["status"] == "KNOWN_UNPUBLISHED_QA_FIXTURE"
    assert SUPPORT_TABLE["schema_version"] == (2,) and SUPPORT_TABLE["database_version"] == (1001,)
    assert SUPPORT_TABLE["real_previous_amount_tool_release_exists"] is False
    with pytest.raises(ValueError, match="版本"):
        Catalog(root)
    result = migrate_amount_catalog(root, tmp_path / "preupgrade")
    assert result["status"] == "MIGRATED_QA_FIXTURE"
    assert result["from_version"] == 1000 and result["to_version"] == 1001
    assert result["real_previous_amount_tool_release"] is False
    snapshot = Path(result["snapshot_path"])
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == result["snapshot_sha256"]
    assert inspect_amount_catalog(snapshot)["database_version"] == 1000
    assert inspect_amount_catalog(root)["database_version"] == 1001
    assert _fixed_and_reports(root) == fixed
    catalog = Catalog(root)
    row = catalog.get(batch["batch_id"])
    assert row["per_code_expected_amount"] == "200000000000000022"
    assert row["total_amount"] == "200000000000000022000"
    with catalog.connection() as connection:
        assert connection.execute("SELECT typeof(total_amount),typeof(per_code_expected_amount) FROM batches").fetchone()[:] == ("text", "text")
        assert all(record[0] == "text" for record in connection.execute("SELECT typeof(amount) FROM batch_sets"))
    (tmp_path / "migration-result.json").write_bytes(canonical_json({"result": result, "fixed_and_reports_unchanged": True}))


def test_failed_transaction_retains_original_index_bytes_and_verified_snapshot(compatibility_fixture, tmp_path):
    _, _, legacy, _ = compatibility_fixture
    root = tmp_path / "failure"
    shutil.copytree(legacy, root)
    original = (root / "history.sqlite").read_bytes()
    fixed = _fixed_and_reports(root)
    def fail():
        raise RuntimeError("QA injected migration failure before commit")
    with pytest.raises(RuntimeError, match="migration failure"):
        migrate_amount_catalog(root, tmp_path / "snapshots", before_commit=fail)
    assert (root / "history.sqlite").read_bytes() == original
    assert _fixed_and_reports(root) == fixed
    assert inspect_amount_catalog(root)["database_version"] == 1000
    snapshots = list((tmp_path / "snapshots/catalog/preupgrade").glob("*.sqlite"))
    assert len(snapshots) == 1 and inspect_amount_catalog(snapshots[0])["database_version"] == 1000
    assert not (root / "history.sqlite-journal").exists()


def test_unknown_future_and_unknown_legacy_shape_refuse_before_mkdir(compatibility_fixture, tmp_path):
    _, _, legacy, _ = compatibility_fixture
    for name, sql in (("future", "PRAGMA user_version=1002"),
                      ("unknown-legacy", "ALTER TABLE batches ADD COLUMN mystery TEXT")):
        root = tmp_path / name
        shutil.copytree(legacy, root)
        connection = sqlite3.connect(root / "history.sqlite")
        try:
            connection.execute(sql)
            connection.commit()
        finally:
            connection.close()
        original = (root / "history.sqlite").read_bytes()
        backups = tmp_path / (name + "-not-created")
        with pytest.raises(ValueError):
            migrate_amount_catalog(root, backups)
        assert (root / "history.sqlite").read_bytes() == original and not backups.exists()


def test_readonly_rollback_guard_preserves_post_upgrade_completed_batch(compatibility_fixture, tmp_path):
    _, published, legacy, initial = compatibility_fixture
    root = tmp_path / "rollback"
    shutil.copytree(published, root)
    old = legacy / "history.sqlite"
    assert prevent_destructive_index_rollback(root, old)["performed_write"] is False
    new = generate_amount_batch((9,), root, backup_root=tmp_path / "new-backups")
    assert new["status"] == "COMPLETED" and new["checks_passed"] == 15
    files = _fixed_and_reports(root)
    index = (root / "history.sqlite").read_bytes()
    package = Path(new["backup"]["package_path"])
    package_files = {p.relative_to(package).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in package.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="丢失批次"):
        prevent_destructive_index_rollback(root, old)
    assert (root / "history.sqlite").read_bytes() == index and _fixed_and_reports(root) == files
    assert package_files == {p.relative_to(package).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in package.rglob("*") if p.is_file()}
    assert Catalog(root).get(new["batch_id"])["generation_status"] == "COMPLETED"
    (tmp_path / "rollback-guard.json").write_bytes(canonical_json({"new_batch_preserved": new,
        "old_snapshot": str(old), "overwrite_refused": True, "fixed_and_packages_unchanged": True}))
