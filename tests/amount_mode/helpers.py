"""QA-only candidate construction and resealing, never imported by application."""
from pathlib import Path
import uuid
from number_app.amount_mode.spec import AmountBatchSpec
from number_app.amount_mode.input_config import build_request_bytes
from number_app.amount_mode.artifacts import write_amount_candidate
from number_app.amount_mode.protocol import AuditContext, MODE, CHECKER_VERSION, canonical_json, digest, strict_json


def candidate(root, amounts=(1, 5, 2), leading=()):
    spec = AmountBatchSpec(tuple(amounts))
    used = [i for group in leading for i in group]
    assert len(set(used)) == len(used)
    left = [i for i in range(spec.raw_total) if i not in used]
    sizes = [len(g) for g in leading]
    # Complete deterministic original pool, legal even when a forced prefix leaves an odd remainder.
    cursor = 0
    while cursor < len(left):
        q = 2 if len(left) - cursor != 3 else 3
        sizes.append(q)
        used.extend(left[cursor:cursor + q])
        cursor += q
    bid = uuid.uuid4().hex
    request = build_request_bytes(spec, batch_id=bid)
    result = write_amount_candidate(root, spec, request, sizes, used)
    context = AuditContext(MODE, bid, uuid.uuid4().hex, CHECKER_VERSION, spec.amounts,
                           result["request_sha256"], result["snapshot_digest"], len(sizes))
    return context


def reseal(root, context):
    from dataclasses import replace
    p = Path(root) / "manifest.json"
    manifest = strict_json(p.read_bytes())
    for entry in manifest["files"]:
        data = (Path(root) / entry["path"]).read_bytes()
        entry.update(size=len(data), sha256=digest(data))
    data = canonical_json(manifest)
    p.write_bytes(data)
    return replace(context, expected_manifest_sha256=digest(data))
