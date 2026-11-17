"""Single coordinator: immutable input, fifteen real checks and safe publication."""
from dataclasses import replace
from pathlib import Path
import time
import uuid

from number_app.storage.locking import WorkspaceLock
from .artifacts import CancelledError, write_amount_candidate
from .audit import audit_amount_batch
from .catalog import Catalog
from .input_config import build_request_bytes
from .limits import resource_preflight
from .output_paths import protect_paths
from .partition import generate_group_sizes
from .pool import shuffle_item_ids
from .protocol import AuditContext, MODE, CHECKER_VERSION, digest, assert_snapshot_unchanged
from .spec import AmountBatchSpec

DEFAULT_DATA_ROOT = Path("E:/number/data_amount")
DEFAULT_BACKUP_ROOT = Path("E:/number/data_amount_backups")


class AuditFailed(ValueError):
    def __init__(self, message, *, outcome=None, batch_id=None):
        super().__init__(message)
        self.outcome = outcome
        self.batch_id = batch_id


def _emit(callback, bid, state, **values):
    if callback is not None:
        callback({"batch_id": bid, "state": state, **values})


def _cancelled(cancel):
    if cancel is not None and cancel():
        raise CancelledError("已在提交前取消")


def _record_failure(catalog, bid, error, context=None):
    row = catalog.get(bid)
    if row is None or row["generation_status"] == "COMPLETED":
        return
    if isinstance(error, CancelledError):
        state = "CANCELLED"
    elif isinstance(error, (KeyboardInterrupt, SystemExit)):
        state = "INTERRUPTED"
    else:
        state = "FAILED"
    if context is not None:
        try:
            assert_snapshot_unchanged(catalog.path(bid), context)
        except (ValueError, OSError):
            state = "DAMAGED"
    catalog.transition(bid, state, error=str(error))


def _publish(catalog, bid, outcome, *, cancel=None, progress=None):
    context = outcome.context
    _cancelled(cancel)
    assert_snapshot_unchanged(catalog.path(bid), context)
    _emit(progress, bid, "BEFORE_COMMITTING", audit_id=context.audit_id)
    _cancelled(cancel)
    catalog.transition(bid, "COMMITTING")
    _emit(progress, bid, "COMMITTING", audit_id=context.audit_id)
    # COMMITTING is the ordinary cancellation boundary; finish the transaction now.
    source = catalog.path(bid)
    final = catalog.root / "batches" / bid
    if source != final:
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            raise FileExistsError("正式批次目标已存在，拒绝覆盖")
        source.rename(final)
        catalog.transition(bid, "COMMITTING", batch_relpath=f"batches/{bid}")
    _emit(progress, bid, "PUBLISHED", audit_id=context.audit_id)
    assert_snapshot_unchanged(final, context)
    relocated = replace(outcome, session_dir=final / "audits" / context.audit_id)
    catalog.complete(bid, relocated)
    result = catalog.result_summary(bid, audit_id=context.audit_id)
    _emit(progress, bid, "COMPLETED", audit_id=context.audit_id, result=result)
    return result


def generate_amount_batch(amounts, data_root=DEFAULT_DATA_ROOT, *, backup_root=None, cancel=None, progress=None):
    spec = amounts if type(amounts) is AmountBatchSpec else AmountBatchSpec(amounts)
    backup_root = DEFAULT_BACKUP_ROOT if backup_root is None and Path(data_root).resolve() == DEFAULT_DATA_ROOT else (backup_root or Path(str(data_root) + "_backups"))
    data_root, backup_root = protect_paths(data_root, backup_root, workspace_roots=(data_root,))
    resource_preflight(spec, data_root, backup_root)
    from .runtime import precheck_catalog
    precheck_catalog(data_root)
    started = time.monotonic()
    bid = uuid.uuid4().hex
    request_bytes = build_request_bytes(spec, batch_id=bid)
    context = None
    with WorkspaceLock(data_root):
        catalog = Catalog(data_root)
        catalog.create(spec, request_bytes)
        try:
            _cancelled(cancel)
            catalog.transition(bid, "GENERATING")
            _emit(progress, bid, "GENERATING")
            sizes = generate_group_sizes(spec.raw_total)
            identities = shuffle_item_ids(spec.raw_total)
            _cancelled(cancel)
            catalog.transition(bid, "WRITING")
            _emit(progress, bid, "WRITING")
            frozen = write_amount_candidate(catalog.path(bid), spec, request_bytes, sizes, identities, cancel=cancel)
            context = AuditContext(MODE, bid, uuid.uuid4().hex, CHECKER_VERSION, spec.amounts,
                                   digest(request_bytes), frozen["snapshot_digest"], frozen["group_count"])
            catalog.freeze(bid, context)
            _emit(progress, bid, "FROZEN")
            _cancelled(cancel)
            catalog.transition(bid, "AUDITING")
            outcome = audit_amount_batch(catalog.path(bid), context, cancel=cancel, progress=progress)
            catalog.record_audit(bid, outcome)
            if outcome.status != "PASS":
                if outcome.status == "CANCELLED":
                    raise CancelledError("审查已取消")
                raise AuditFailed("15次审查未全部通过：" + "; ".join(outcome.errors), outcome=outcome, batch_id=bid)
            result = _publish(catalog, bid, outcome, cancel=cancel, progress=progress)
        except BaseException as error:
            _record_failure(catalog, bid, error, context)
            raise
    result["elapsed_seconds"] = time.monotonic() - started
    return finish_batch_backup(result, data_root, backup_root)


def finish_batch_backup(result, data_root, backup_root=None):
    backup_root = backup_root or (DEFAULT_BACKUP_ROOT if Path(data_root).resolve() == DEFAULT_DATA_ROOT else Path(str(data_root) + '_backups'))
    # A backup failure never rolls back a completed number batch or causes regeneration.
    try:
        from .backup import backup_amount_batch
        result["backup"] = backup_amount_batch(data_root, result['batch_id'], backup_root)
        result['backup_status'] = 'COMPLETE'
    except Exception as error:
        result["backup"] = {"status": "FAILED", "error": str(error)}
        result['backup_status'] = 'FAILED'
        try:
            with WorkspaceLock(data_root):
                catalog = Catalog(data_root)
                row = catalog.get(result['batch_id'])
                if row['backup_status'] != 'FAILED':
                    catalog.record_backup(result['batch_id'], uuid.uuid4().hex, manifest_sha256=row['manifest_sha256'],
                                          audit_ids=[], status='FAILED', error=str(error))
        except Exception as ledger_error:
            result['backup']['ledger_error'] = str(ledger_error)
    return result
