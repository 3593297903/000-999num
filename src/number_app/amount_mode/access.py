"""Read-only authentication before a completed result is taken into use."""
from .protocol import assert_snapshot_unchanged


def require_usable(catalog, bid):
    row = catalog.get(bid)
    if row is None or row['generation_status'] != 'COMPLETED' or row['latest_verification_status'] != 'PASS':
        raise ValueError('本批未完成或最近完整复核未通过，暂停取用')
    context = catalog.context(bid)
    try:
        assert_snapshot_unchanged(catalog.path(bid), context)
    except (OSError, ValueError) as error:
        catalog.transition(bid, 'DAMAGED', error=str(error))
        raise
    record = catalog.audit_record(row['latest_audit_id'])
    try:
        catalog._validated_session(bid, context.with_audit(record['audit_id']),
                                   catalog._checks_for_validation(record), record['summary_sha256'])
    except (OSError, ValueError) as error:
        catalog.mark_verification(bid, 'ERROR', error=str(error))
        raise
    return context
