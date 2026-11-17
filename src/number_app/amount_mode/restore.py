"""Restore from self-contained anchors; only target copies receive new checks."""
from pathlib import Path, PurePosixPath
import uuid

from number_app.storage.locking import WorkspaceLock
from .artifacts import CancelledError
from .audit import AuditOutcome, audit_amount_batch
from .backup import _copy_verified, _checks, validate_amount_backup
from .catalog import Catalog
from .limits import resource_preflight
from .output_paths import protect_paths
from .protocol import canonical_json, read_snapshot, strict_json
from .service import AuditFailed, _publish, _record_failure


def _empty_target(target):
    if target.exists() and (not target.is_dir() or next(target.iterdir(), None) is not None):
        raise ValueError("恢复目标必须是全新或明确为空的独立数据根")


def _restore_one(validated, catalog, *, cancel=None, progress=None):
    bid = validated["batch_id"]
    if catalog.get(bid) is not None:
        raise ValueError("目标已有同批次身份，拒绝覆盖或合并")
    spec, context = validated["spec"], validated["context"]
    resource_preflight(spec, catalog.root, catalog.root)
    catalog.create(spec, canonical_json(validated["anchor"]["original_request"]))
    try:
        catalog.transition(bid, "GENERATING")
        catalog.transition(bid, "WRITING")
        target = catalog.path(bid)
        target.mkdir(parents=True, exist_ok=False)
        for relative, entry in validated["entries"].items():
            package_relative = PurePosixPath(relative)
            if package_relative.parts[0] != "batch":
                continue
            if cancel is not None and cancel():
                raise CancelledError("恢复复制已取消，尚未发布")
            name = package_relative.relative_to("batch").as_posix()
            _copy_verified(validated["root"] / relative, target / name, entry["sha256"], entry["size"])
        unchanged_package = validate_amount_backup(validated["root"])
        if unchanged_package["backup_manifest_sha256"] != validated["backup_manifest_sha256"]:
            raise ValueError("恢复复制期间备份包发生变化")
        read_snapshot(target, context)
        catalog.freeze(bid, context)
        # Preserve committed historic evidence. None of these old sessions is
        # allowed to complete the restored batch; a fresh 15 follows below.
        for record, checks in validated["audit_records"]:
            errors = strict_json(record["errors_json"])
            historic = AuditOutcome(context.with_audit(record["audit_id"]), record["status"], tuple(checks),
                                    target / "audits" / record["audit_id"], record["summary_sha256"], tuple(errors),
                                    15 if record["status"] == "PASS" else 0)
            catalog.record_audit(bid, historic)
        catalog.transition(bid, "AUDITING")
        outcome = audit_amount_batch(target, context, cancel=cancel, progress=progress)
        catalog.record_audit(bid, outcome)
        if outcome.status != "PASS":
            if outcome.status == "CANCELLED":
                raise CancelledError("恢复核查已取消")
            raise AuditFailed("恢复副本15次核查未全部通过", outcome=outcome, batch_id=bid)
        result = _publish(catalog, bid, outcome, cancel=cancel, progress=progress)
        catalog.record_backup(bid, validated["backup_id"], manifest_sha256=context.expected_manifest_sha256,
                              audit_ids=validated["covered_audit_ids"], package_path=validated["root"])
        result.update(restored_from_backup_id=validated["backup_id"],
                      source_backup_manifest_sha256=validated["backup_manifest_sha256"],
                      verified_target_root=str(catalog.root), backup_status="COMPLETE")
        return result
    except BaseException as error:
        row = catalog.get(bid)
        # A partial copy has no trusted manifest yet and must remain incomplete,
        # not be authenticated using the source package's otherwise-good data.
        _record_failure(catalog, bid, error, context if row["manifest_sha256"] else None)
        raise


def restore_amount_backup(package_path, target_root, *, cancel=None, progress=None):
    target, = protect_paths(target_root, workspace_roots=(target_root,))
    _empty_target(target)
    validated = validate_amount_backup(package_path)
    resource_preflight(validated["spec"], target, target)
    with WorkspaceLock(target):
        catalog = Catalog(target)
        return _restore_one(validated, catalog, cancel=cancel, progress=progress)


def restore_amount_backups(package_paths, target_root, *, cancel=None, progress=None):
    """Fresh-root bulk recovery; each package reports its own new 15-check result."""
    target, = protect_paths(target_root, workspace_roots=(target_root,))
    _empty_target(target)
    packages = list(package_paths)
    if not packages:
        raise ValueError("请至少选择一个完整批次包")
    prepared, failures, identities = [], [], {}
    for package in packages:
        try:
            valid = validate_amount_backup(package)
            bid, fingerprint = valid["batch_id"], valid["context"].expected_manifest_sha256
            if bid in identities:
                raise ValueError("多包同批次重复或原指纹冲突，拒绝合并")
            identities[bid] = fingerprint
            prepared.append(valid)
        except (ValueError, OSError) as error:
            failures.append({"package_path": str(package), "status": "FAILED", "error": str(error)})
    if not prepared:
        return {"status": "FAILED", "batches": failures, "completed": 0, "failed": len(failures)}
    results = list(failures)
    was_cancelled = False
    def append_not_started(remaining):
        for pending in remaining:
            results.append({'batch_id': pending['batch_id'], 'status': 'CANCELLED', 'error': '取消后未开始，不登记目标批次'})
    if cancel is not None and cancel():
        append_not_started(prepared)
        return {'status': 'CANCELLED', 'batches': results, 'completed': 0, 'failed': len(failures), 'cancelled': len(prepared)}
    with WorkspaceLock(target):
        catalog = Catalog(target)
        for index, valid in enumerate(prepared):
            if cancel is not None and cancel():
                was_cancelled = True
                append_not_started(prepared[index:])
                break
            try:
                results.append(_restore_one(valid, catalog, cancel=cancel, progress=progress))
            except CancelledError as error:
                was_cancelled = True
                results.append({'batch_id': valid['batch_id'], 'status': 'CANCELLED', 'error': str(error)})
                append_not_started(prepared[index + 1:])
                break
            except Exception as error:
                results.append({"batch_id": valid["batch_id"], "status": "FAILED", "error": str(error)})
    complete = sum(result["status"] == "COMPLETED" for result in results)
    return {"status": 'CANCELLED' if was_cancelled else ("COMPLETED" if complete == len(results) else "PARTIAL"), "batches": results,
            "completed": complete, "failed": sum(r['status'] == 'FAILED' for r in results),
            "cancelled": sum(r['status'] == 'CANCELLED' for r in results), "verified_target_root": str(target)}
