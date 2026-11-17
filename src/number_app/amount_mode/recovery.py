"""Recheck original anchors; never regenerate or establish replacement trust."""
from pathlib import Path
from number_app.storage.locking import WorkspaceLock
from .artifacts import CancelledError
from .audit import audit_amount_batch
from .catalog import Catalog
from .output_paths import protect_paths
from .protocol import assert_snapshot_unchanged
from .service import AuditFailed, _publish, _emit, finish_batch_backup


def _locate(catalog, bid):
    current = catalog.path(bid)
    if not current.exists() and (catalog.root / "batches" / bid).exists():
        # A hard exit can occur after rename, before the relative index is updated.
        catalog.transition(bid, "INTERRUPTED", batch_relpath=f"batches/{bid}")
        current = catalog.path(bid)
    return current


def recover_amount_batch(data_root, batch_id, *, cancel=None, progress=None, backup_root=None):
    root, = protect_paths(data_root, workspace_roots=(data_root,))
    with WorkspaceLock(root):
        catalog = Catalog(root)
        row = catalog.get(batch_id)
        if row is None or row["generation_status"] == "COMPLETED":
            raise ValueError("请选择未完成且有可信完整快照的批次；已完成批次请独立复核")
        path = _locate(catalog, batch_id)
        if catalog.get(batch_id)["generation_status"] in ("AUDITING", "COMMITTING"):
            catalog.transition(batch_id, "INTERRUPTED", error="上次协调进程在审查或提交中结束，重新完整核查")
        if not row["manifest_sha256"]:
            if catalog.get(batch_id)['generation_status'] not in ('FAILED', 'CANCELLED', 'INTERRUPTED', 'DAMAGED'):
                catalog.transition(batch_id, "INTERRUPTED", error="没有首次可信manifest，不能从现有文件重新认定基准")
            raise AuditFailed("缺少首次可信快照；保留现场，另建新批次")
        context = catalog.context(batch_id)
        try:
            assert_snapshot_unchanged(path, context)
        except (OSError, ValueError) as error:
            catalog.transition(batch_id, "DAMAGED", error=str(error))
            raise AuditFailed(str(error), batch_id=batch_id) from error
        catalog.transition(batch_id, "AUDITING")
        _emit(progress, batch_id, "AUDITING")
        outcome = audit_amount_batch(path, context, cancel=cancel, progress=progress)
        catalog.record_audit(batch_id, outcome)
        if outcome.status != "PASS":
            catalog.transition(batch_id, "CANCELLED" if outcome.status == "CANCELLED" else "INTERRUPTED", error="恢复重新核查未通过")
            raise AuditFailed("恢复15次审查未通过", outcome=outcome, batch_id=batch_id)
        result = _publish(catalog, batch_id, outcome, cancel=cancel, progress=progress)
    return finish_batch_backup(result, root, backup_root)


def verify_amount_batch(batch_dir, data_root, *, cancel=None, progress=None):
    root, = protect_paths(data_root, workspace_roots=(data_root,))
    target = Path(batch_dir).resolve(strict=True)
    with WorkspaceLock(root):
        catalog = Catalog(root)
        from .input_config import validate_request
        from .protocol import strict_json, AuditContext, MODE, CHECKER_VERSION, digest
        import uuid
        # Read identity only; local anchors take precedence over any external declaration.
        def limited(name, maximum):
            with (target / name).open("rb") as stream:
                data = stream.read(maximum + 1)
            if len(data) > maximum:
                raise ValueError("外部文件超过支持上限：" + name)
            return data
        request_bytes = limited("request.json", 60000)
        request = strict_json(request_bytes)
        spec = validate_request(request)
        bid = request["batch_id"]
        row = catalog.get(bid)
        registered = catalog.get_by_path(target)
        if registered is not None and registered["batch_id"] != bid:
            catalog.transition(registered["batch_id"], "DAMAGED", error="已登记目录中的请求身份被修改")
            raise AuditFailed("本地已登记批次的请求身份不匹配")
        if row is not None:
            if target != catalog.path(bid):
                raise AuditFailed("本地已登记同批次，请选择其原登记目录；外部同身份不能替代或降级声明")
            context = catalog.context(bid)
        else:
            plan = strict_json(limited("group_plan.json", spec.raw_total * 30 + 1000))
            context = AuditContext(MODE, bid, uuid.uuid4().hex, CHECKER_VERSION, spec.amounts,
                                   digest(request_bytes), digest(limited("manifest.json", 4096)),
                                   plan["group_count"], "imported_declaration_only")
        if row is not None:
            catalog.mark_verification(bid, "RUNNING")
        try:
            outcome = audit_amount_batch(target, context, cancel=cancel, progress=progress)
            if row is not None:
                # Native reports are always stored with the native fixed snapshot.
                catalog.record_audit(bid, outcome)
                catalog.mark_verification(bid, outcome.status, audit_id=outcome.context.audit_id)
                try:
                    assert_snapshot_unchanged(target, context)
                except (ValueError, OSError) as error:
                    catalog.transition(bid, "DAMAGED", error=str(error))
            if outcome.status != "PASS":
                raise AuditFailed("独立复核未全部通过", outcome=outcome, batch_id=bid)
            result = {"status": "PASS", "mode": MODE, "batch_id": bid, "batch_dir": str(target),
                      "audit_id": outcome.context.audit_id, "checks_passed": 15, "provenance": context.provenance,
                      "request_sha256": context.expected_request_sha256, "snapshot_digest": context.expected_manifest_sha256}
            return result
        except BaseException as error:
            if row is not None:
                status = error.outcome.status if isinstance(error, AuditFailed) and error.outcome is not None else ("CANCELLED" if isinstance(error, (CancelledError, KeyboardInterrupt)) else "ERROR")
                catalog.mark_verification(bid, status, error=str(error))
                try:
                    assert_snapshot_unchanged(target, context)
                except (ValueError, OSError):
                    catalog.transition(bid, "DAMAGED", error=str(error))
            raise
