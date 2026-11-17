"""Explicit first-release compatibility and a single unpublished QA migration.

Index 1000 was never an AmountTool release. It is a documented migration fixture
formed from this release's exact schema by dropping backup_status/backup_error.
No unknown schema is guessed, repaired, recreated, or automatically migrated.
"""
import hashlib
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from number_app.storage.locking import WorkspaceLock
from .catalog import APPLICATION_ID, DATABASE_VERSION, Catalog, _SCHEMA
from .output_paths import protect_paths
from .protocol import CHECKER_VERSION, MODE, VERSIONS, canonical_json, synced_write

SUPPORT_TABLE = {
    "mode": (MODE,), "schema_version": (2,),
    **{key: (value,) for key, value in VERSIONS.items()},
    "checker_version": (CHECKER_VERSION,), "report_version": (1,),
    "summary_version": (1,), "backup_version": (1,),
    "database_version": (DATABASE_VERSION,),
    "qa_migration_source_database_version": (1000,),
    "real_previous_amount_tool_release_exists": False,
}
LEGACY_FIXTURE_VERSION = 1000
LEGACY_FIXTURE_PROVENANCE = "unpublished_qa_fixture_not_a_previous_amount_tool_release"


def _digest_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _normalize_sql(value):
    """Only normalize whitespace outside SQL quoted literals/identifiers."""
    if value is None:
        return None
    parts = re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^]]*\]|\s+|[^\s'\"`\[]+", value)
    return "".join(" " if part.isspace() else part for part in parts).strip()


def _schema_signature(connection):
    objects = tuple((kind, name, _normalize_sql(sql)) for kind, name, sql in connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"))
    tables = [name for kind, name, _ in objects if kind == "table"]
    columns = tuple((table, tuple(tuple(row) for row in connection.execute('PRAGMA table_info("' + table + '")')))
                    for table in tables)
    relations = tuple((table, tuple(tuple(row) for row in connection.execute('PRAGMA foreign_key_list("' + table + '")')))
                      for table in tables)
    return objects, columns, relations


@lru_cache(maxsize=1)
def _known_schemas():
    """Create only an in-memory declaration, never a target/catalog file."""
    connection = sqlite3.connect(":memory:")
    try:
        Catalog._configure(connection, set())
        connection.executescript(_SCHEMA)
        published = _schema_signature(connection)
        connection.execute("ALTER TABLE batches DROP COLUMN backup_status")
        connection.execute("ALTER TABLE batches DROP COLUMN backup_error")
        legacy = _schema_signature(connection)
        connection.execute("ALTER TABLE batches ADD COLUMN backup_status TEXT NOT NULL DEFAULT 'PENDING'")
        connection.execute("ALTER TABLE batches ADD COLUMN backup_error TEXT")
        migrated = _schema_signature(connection)
        return published, legacy, migrated
    finally:
        connection.close()


def _index_path(root_or_file):
    value = Path(root_or_file)
    index = value if value.is_file() or value.suffix.lower() == ".sqlite" else value / "history.sqlite"
    if index.is_symlink() or index.is_junction() or not index.is_file() or index.stat().st_size == 0:
        raise ValueError("索引缺失、为空或为链接，未创建/改写任何数据库")
    return index.resolve(strict=True)


@contextmanager
def _readonly(index):
    connection = sqlite3.connect(Path(index).resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        yield connection
    finally:
        connection.close()


def _recognize(connection):
    application = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if application != APPLICATION_ID:
        raise ValueError("不是AMTL索引；禁止迁移、重建或覆盖")
    if version not in (LEGACY_FIXTURE_VERSION, DATABASE_VERSION):
        raise ValueError("不支持的未来或未知索引版本；仅只读拒绝")
    if [tuple(row) for row in connection.execute("PRAGMA quick_check").fetchall()] != [("ok",)]:
        raise ValueError("索引完整性失败，保留原库")
    published, legacy, migrated = _known_schemas()
    expected = (legacy,) if version == LEGACY_FIXTURE_VERSION else (published, migrated)
    if _schema_signature(connection) not in expected:
        raise ValueError("索引表、列、索引或trigger不符合明确支持的精确结构，拒绝猜测迁移")
    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {name: connection.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0] for name in tables}
    return {"application_id": application, "database_version": version, "table_counts": counts,
            "status": "SUPPORTED_CURRENT" if version == DATABASE_VERSION else "KNOWN_UNPUBLISHED_QA_FIXTURE",
            "provenance": "current_first_release" if version == DATABASE_VERSION else LEGACY_FIXTURE_PROVENANCE}


def inspect_amount_catalog(root_or_file):
    """Read-only explicit version recognition; it never instantiates Catalog."""
    index = _index_path(root_or_file)
    try:
        with _readonly(index) as connection:
            result = _recognize(connection)
    except sqlite3.DatabaseError as error:
        raise ValueError("索引不可识别或损坏，未改写：" + str(error)) from error
    return {**result, "index_path": str(index), "index_sha256": _digest_file(index),
            "real_previous_amount_tool_release": False}


def migrate_amount_catalog(root, backup_root, *, before_commit=None):
    """Migrate only the declared QA 1000 fixture, after a restorable snapshot.

    before_commit is a zero-argument fault callback used by Python QA. No CLI
    option exposes it, and it receives no database connection or trust grant.
    """
    if before_commit is not None and not callable(before_commit):
        raise ValueError("before_commit必须是QA回调或None")
    source, backups = protect_paths(root, backup_root, workspace_roots=(root,))
    index = _index_path(source)
    initial = inspect_amount_catalog(index)  # recognition before lock/mkdir
    if initial["database_version"] == DATABASE_VERSION:
        return {"status": "CURRENT", "from_version": DATABASE_VERSION, "to_version": DATABASE_VERSION,
                "index_path": str(index), "performed_write": False, "snapshot_path": None,
                "real_previous_amount_tool_release": False}
    identifier = uuid.uuid4().hex
    snapshot = backups / "catalog/preupgrade" / ("amount-index-before-1000-" + identifier + ".sqlite")
    temporary = snapshot.with_suffix(".sqlite.partial")
    sidecar = snapshot.with_suffix(".json")
    checked = protect_paths(snapshot, temporary, sidecar, workspace_roots=(source,),
                            file_paths=(snapshot, temporary, sidecar))
    if tuple(checked) != (snapshot, temporary, sidecar):
        raise ValueError("迁移快照派生路径包含链接，已停止")
    with WorkspaceLock(source):
        current = inspect_amount_catalog(index)
        if current["database_version"] != LEGACY_FIXTURE_VERSION or current["index_sha256"] != initial["index_sha256"]:
            raise ValueError("迁移前索引发生变化，请重新检查")
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("xb"):
            pass
        target = sqlite3.connect(temporary, isolation_level=None)
        try:
            with _readonly(index) as original:
                original.backup(target)
        finally:
            target.close()
        copied = inspect_amount_catalog(temporary)
        if copied["database_version"] != LEGACY_FIXTURE_VERSION or copied["table_counts"] != current["table_counts"]:
            raise ValueError("升级前索引副本与原库不一致；未迁移")
        with temporary.open("r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        temporary.rename(snapshot)
        snapshot_sha = _digest_file(snapshot)
        metadata = {"snapshot_version": 1, "kind": "preupgrade_history_index_only_not_batch_backup",
                    "mode": MODE, "application_id": APPLICATION_ID,
                    "from_version": LEGACY_FIXTURE_VERSION, "to_version": DATABASE_VERSION,
                    "fixture_provenance": LEGACY_FIXTURE_PROVENANCE, "status": "VERIFIED",
                    "snapshot_filename": snapshot.name, "snapshot_sha256": snapshot_sha,
                    "source_index_sha256_before": current["index_sha256"], "table_counts": current["table_counts"]}
        synced_write(sidecar, canonical_json(metadata))
        connection = sqlite3.connect(index, isolation_level=None)
        try:
            Catalog._configure(connection, set())
            connection.execute("BEGIN IMMEDIATE")
            if _recognize(connection)["database_version"] != LEGACY_FIXTURE_VERSION:
                raise ValueError("索引在迁移事务前变化")
            connection.execute("ALTER TABLE batches ADD COLUMN backup_status TEXT NOT NULL DEFAULT 'PENDING'")
            connection.execute("ALTER TABLE batches ADD COLUMN backup_error TEXT")
            connection.execute(f"PRAGMA user_version={DATABASE_VERSION}")
            if before_commit is not None:
                before_commit()
            _recognize(connection)
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        verified = inspect_amount_catalog(index)
        if verified["database_version"] != DATABASE_VERSION or verified["table_counts"] != current["table_counts"]:
            raise ValueError("迁移后索引验证失败；保留原快照，停止取用")
        return {"status": "MIGRATED_QA_FIXTURE", "from_version": LEGACY_FIXTURE_VERSION,
                "to_version": DATABASE_VERSION, "index_path": str(index), "index_sha256": verified["index_sha256"],
                "snapshot_path": str(snapshot), "snapshot_sha256": snapshot_sha, "snapshot_metadata": str(sidecar),
                "fixture_provenance": LEGACY_FIXTURE_PROVENANCE, "real_previous_amount_tool_release": False}


def prevent_destructive_index_rollback(current, snapshot):
    """Read-only loss detection; this function never replaces any index file."""
    current_file, old_file = _index_path(current), _index_path(snapshot)
    inspect_amount_catalog(current_file)
    inspect_amount_catalog(old_file)
    with _readonly(current_file) as live, _readonly(old_file) as old:
        columns = "batch_id,request_sha256,manifest_sha256,original_request_json,frozen_input_json"
        now_batches = {row[0]: row[1:] for row in live.execute("SELECT " + columns + " FROM batches")}
        old_batches = {row[0]: row[1:] for row in old.execute("SELECT " + columns + " FROM batches")}
        missing_batches = sorted(now_batches.keys() - old_batches.keys())
        conflicting = sorted(bid for bid in now_batches.keys() & old_batches.keys() if now_batches[bid] != old_batches[bid])
        missing_audits = sorted({r[0] for r in live.execute("SELECT audit_id FROM audit_runs")} - {r[0] for r in old.execute("SELECT audit_id FROM audit_runs")})
        missing_backups = sorted({r[0] for r in live.execute("SELECT backup_id FROM backup_runs")} - {r[0] for r in old.execute("SELECT backup_id FROM backup_runs")})
    if missing_batches or conflicting or missing_audits or missing_backups:
        raise ValueError("禁止用旧索引覆盖当前历史：将丢失批次=" + repr(missing_batches)
                         + "，原锚点冲突=" + repr(conflicting) + "，将丢失审查=" + repr(missing_audits)
                         + "，将丢失备份账本=" + repr(missing_backups))
    return {"status": "NO_INDEX_HISTORY_LOSS_DETECTED", "current_index": str(current_file),
            "snapshot_index": str(old_file), "batch_count": len(now_batches), "performed_write": False,
            "note": "仅只读评估；本入口不覆盖索引，恢复取用仍需原锚点和完整15次核查"}
