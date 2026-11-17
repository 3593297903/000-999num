"""Catalog evidence uses new QA only; no old catalog is opened for writing."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import uuid
import pytest

from number_app.amount_mode.artifacts import write_amount_candidate
from number_app.amount_mode.audit import audit_amount_batch
from number_app.amount_mode.catalog import APPLICATION_ID, DATABASE_VERSION, Catalog
from number_app.amount_mode.input_config import build_request_bytes
from number_app.amount_mode.protocol import AuditContext, CHECKER_VERSION, MODE, digest
from number_app.amount_mode.spec import AmountBatchSpec


def _created(catalog, amounts=(1, 5, 2)):
    spec = AmountBatchSpec(amounts)
    bid = uuid.uuid4().hex
    raw = build_request_bytes(spec, batch_id=bid)
    catalog.create(spec, raw)
    return bid, spec, raw


def _frozen(catalog, amounts=(1, 5, 2)):
    bid, spec, raw = _created(catalog, amounts)
    catalog.transition(bid, "GENERATING")
    catalog.transition(bid, "WRITING")
    result = write_amount_candidate(catalog.path(bid), spec, raw, [2] * (spec.raw_total // 2), list(range(spec.raw_total)))
    ctx = AuditContext(MODE, bid, uuid.uuid4().hex, CHECKER_VERSION, amounts,
                       digest(raw), result["snapshot_digest"], result["group_count"])
    catalog.freeze(bid, ctx)
    return bid, ctx


def test_exact_money_persistent_before_files_and_sql_immutable(tmp_path):
    catalog = Catalog(tmp_path / "data")
    amounts = (100000000000000003, 100000000000000019)
    bid, spec, raw = _created(catalog, amounts)
    assert not catalog.path(bid).exists()
    row = catalog.get(bid)
    assert row["original_request_json"].encode() == raw
    assert row["request_sha256"] == digest(raw)
    with catalog.connection() as connection:
        assert connection.execute("SELECT typeof(total_amount),total_amount FROM batches").fetchone()[:] == ("text", str(spec.total_amount))
        assert connection.execute("SELECT typeof(amount) FROM batch_sets LIMIT 1").fetchone()[0] == "text"
    statements = [
        ("UPDATE batches SET request_sha256=? WHERE batch_id=?", ("0" * 64, bid)),
        ("UPDATE batches SET original_request_json=? WHERE batch_id=?", ("{}", bid)),
        ("UPDATE batches SET frozen_input_json=? WHERE batch_id=?", ("{}", bid)),
        ("UPDATE batch_sets SET amount='9' WHERE batch_id=?", (bid,)),
        ("DELETE FROM batch_sets WHERE batch_id=?", (bid,)),
        ("DELETE FROM batches WHERE batch_id=?", (bid,)),
        ("INSERT OR REPLACE INTO batch_sets(batch_id,set_no,amount) VALUES (?,1,'9')", (bid,)),
    ]
    for sql, args in statements:
        with pytest.raises(sqlite3.DatabaseError):
            with catalog.connection(write=True) as connection:
                connection.execute(sql, args)
    with pytest.raises(sqlite3.DatabaseError):
        with sqlite3.connect(catalog.db_path) as connection:
            connection.execute("UPDATE batches SET manifest_sha256='" + "0" * 64 + "' WHERE batch_id=?", (bid,))
    with pytest.raises(ValueError): catalog.set_setting("frozen_input_json", "{}")
    assert catalog.get(bid) == row
    catalog._original(bid)


def test_first_freeze_exact_files_and_no_reanchoring(tmp_path):
    catalog = Catalog(tmp_path / "data")
    bid, context = _frozen(catalog, (1,))
    assert catalog.get(bid)["generation_status"] == "FROZEN"
    assert catalog.context(bid).expected_manifest_sha256 == context.expected_manifest_sha256
    with pytest.raises(ValueError): catalog.freeze(bid, context)
    with pytest.raises(sqlite3.DatabaseError):
        with catalog.connection(write=True) as connection:
            connection.execute("UPDATE batches SET manifest_sha256=? WHERE batch_id=?", ("0" * 64, bid))
    with pytest.raises(sqlite3.DatabaseError):
        with catalog.connection(write=True) as connection:
            connection.execute("UPDATE artifacts SET sha256=? WHERE batch_id=?", ("0" * 64, bid))
    second, spec, raw = _created(catalog, (1,))
    catalog.transition(second, "GENERATING")
    catalog.transition(second, "WRITING")
    candidate = replace(context, batch_id=second, expected_request_sha256=digest(raw))
    with pytest.raises((ValueError, FileNotFoundError)): catalog.freeze(second, candidate)
    assert catalog.get(second)["manifest_sha256"] is None
    with pytest.raises(ValueError): catalog.context(second)


def test_actual_fifteen_reports_required_for_published_completion(tmp_path):
    catalog = Catalog(tmp_path / "data")
    bid, context = _frozen(catalog, (1,))
    catalog.transition(bid, "AUDITING")
    fake = SimpleNamespace(context=context, status="PASS", checks=(), checks_passed=15,
                           summary_sha256="0" * 64, errors=())
    with pytest.raises(ValueError): catalog.record_audit(bid, fake)
    with pytest.raises(sqlite3.DatabaseError):
        with catalog.connection(write=True) as connection:
            connection.execute("UPDATE batches SET generation_status='COMPLETED' WHERE batch_id=?", (bid,))
    outcome = audit_amount_batch(catalog.path(bid), context)
    assert outcome.passed, outcome.errors
    record = catalog.record_audit(bid, outcome)
    assert len(record["checks"]) == 15 and record["status"] == "PASS"
    with pytest.raises(ValueError): catalog.complete(bid, outcome)
    catalog.transition(bid, "COMMITTING")
    final = catalog.root / "batches" / bid
    final.parent.mkdir()
    catalog.path(bid).rename(final)
    catalog.transition(bid, "COMMITTING", batch_relpath="batches/" + bid)
    catalog.complete(bid, replace(outcome, session_dir=final / "audits" / outcome.context.audit_id))
    summary = catalog.result_summary(bid)
    assert summary["status"] == "COMPLETED" and summary["checks_passed"] == 15
    assert catalog.get_by_path(final)["batch_id"] == bid
    catalog.mark_verification(bid, "CANCELLED", error="new review cancelled")
    assert catalog.get(bid)["generation_status"] == "COMPLETED"
    assert catalog.result_summary(bid)["checks_passed"] == 0
    with pytest.raises(ValueError): catalog.mark_verification(bid, "PASS", audit_id=outcome.context.audit_id)
    fresh = audit_amount_batch(final, catalog.context(bid))
    assert fresh.passed, fresh.errors
    catalog.record_audit(bid, fresh)
    catalog.mark_verification(bid, "PASS", audit_id=fresh.context.audit_id)
    assert catalog.result_summary(bid)["checks_passed"] == 15
    # Changed actual report bytes cannot be accepted through a stored PASS index.
    report = final / "audits" / fresh.context.audit_id / "A_1.json"
    report.write_bytes(report.read_bytes() + b"\n")
    with pytest.raises(ValueError): catalog.mark_verification(bid, "PASS", audit_id=fresh.context.audit_id)


def test_invalid_databases_preserved_and_missing_index_not_silently_rebuilt(tmp_path):
    for index, contents in enumerate((b"", b"not sqlite")):
        root = tmp_path / str(index)
        root.mkdir()
        db = root / "history.sqlite"
        db.write_bytes(contents)
        before = {p.name: p.read_bytes() for p in root.iterdir()}
        with pytest.raises(ValueError): Catalog(root)
        assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    root = tmp_path / "foreign"
    root.mkdir()
    with sqlite3.connect(root / "history.sqlite") as connection:
        connection.execute("CREATE TABLE old_data(x)")
    before = (root / "history.sqlite").read_bytes()
    with pytest.raises(ValueError): Catalog(root)
    assert (root / "history.sqlite").read_bytes() == before
    valid = Catalog(tmp_path / "future")
    with sqlite3.connect(valid.db_path) as connection:
        connection.execute("PRAGMA user_version=1002")
    before = valid.db_path.read_bytes()
    with pytest.raises(ValueError): Catalog(valid.root)
    assert valid.db_path.read_bytes() == before
    missing = tmp_path / "missing"
    (missing / "batches" / "evidence").mkdir(parents=True)
    with pytest.raises(ValueError): Catalog(missing)
    assert not (missing / "history.sqlite").exists()


def test_relative_paths_state_lock_and_indexed_pagination(tmp_path):
    catalog = Catalog(tmp_path / "data")
    bids = [_created(catalog, (1,))[0] for _ in range(5)]
    assert catalog.count() == 5
    first = catalog.list_page(limit=2)
    second = catalog.list_page(offset=2, limit=2)
    assert len(first) == len(second) == 2
    assert not {r["batch_id"] for r in first} & {r["batch_id"] for r in second}
    assert all("original_request_json" not in r for r in first)
    with catalog.connection() as connection:
        query = connection.execute("EXPLAIN QUERY PLAN SELECT batch_id FROM batches ORDER BY created_at DESC,batch_id DESC LIMIT 2").fetchall()
    assert any("INDEX" in r[3] for r in query)
    bid = bids[0]
    with pytest.raises(ValueError): catalog.transition(bid, "COMMITTING")
    with pytest.raises(ValueError): catalog.transition(bid, "GENERATING", batch_relpath="../outside")
    with pytest.raises(sqlite3.DatabaseError):
        with catalog.connection(write=True) as connection:
            connection.execute("UPDATE batches SET batch_relpath=? WHERE batch_id=?", (str(tmp_path), bid))
    for _ in range(20): catalog.list_page(limit=2)
    # Windows rename succeeds only after all SQLite handles have actually closed.
    moved = catalog.db_path.with_name("closed.sqlite")
    catalog.db_path.rename(moved)
    moved.rename(catalog.db_path)
    assert Catalog(catalog.root).count() == 5
