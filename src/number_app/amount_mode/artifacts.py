"""Write one owned candidate; request bytes are fixed before this function."""
from pathlib import Path
import os

from .formatting import format_amount_group
from .input_config import validate_request
from .protocol import MODE, canonical_json, digest, strict_json, synced_write


class CancelledError(Exception):
    pass


def write_amount_candidate(destination, spec, request_bytes, group_sizes, item_ids, *, cancel=None):
    request = strict_json(request_bytes)
    if validate_request(request) != spec or canonical_json(request) != request_bytes:
        raise ValueError("候选写入器必须采用协调者已固定的规范请求")
    if len(item_ids) != spec.raw_total or sum(group_sizes) != spec.raw_total or any(type(q) is not int or not 2 <= q <= 5 for q in group_sizes):
        raise ValueError("完整编号数量或组计划不合法")
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=False)
    synced_write(root / "request.json", request_bytes)
    bid = request["batch_id"]
    plan = {"schema_version": 2, "mode": MODE, "batch_id": bid, "group_count": len(group_sizes),
            "groups": [{"group": g, "raw_count": q} for g, q in enumerate(group_sizes, 1)]}
    synced_write(root / "group_plan.json", canonical_json(plan))
    index = 0
    with (root / "items.jsonl").open("xb") as details, (root / "groups.txt").open("xb") as output:
        for group, size in enumerate(group_sizes, 1):
            if cancel is not None and cancel():
                raise CancelledError("生成已取消，保留未完成候选")
            records = []
            for position in range(1, size + 1):
                item_id = item_ids[index]
                index += 1
                if type(item_id) is not int or not 0 <= item_id < spec.raw_total:
                    raise ValueError("编号不在完整池范围")
                row = {"group": group, "position": position, "item_id": item_id,
                       "set_no": item_id // 1000 + 1, "code": f"{item_id % 1000:03d}",
                       "amount": spec.amounts[item_id // 1000]}
                records.append(row)
                details.write(canonical_json({**row, "amount": str(row["amount"])}))
            output.write((("\n" if group > 1 else "") + f"第{group}组\n" + format_amount_group(records)).encode("utf-8"))
        for stream in (details, output):
            stream.flush()
            os.fsync(stream.fileno())
    manifest = {"schema_version": 2, "mode": MODE, "batch_id": bid, "files": []}
    for name in ("request.json", "group_plan.json", "items.jsonl", "groups.txt"):
        data = (root / name).read_bytes()
        manifest["files"].append({"path": name, "size": len(data), "sha256": digest(data)})
    manifest_bytes = canonical_json(manifest)
    synced_write(root / "manifest.json", manifest_bytes)
    return {"batch_id": bid, "group_count": len(group_sizes), "request_sha256": digest(request_bytes),
            "snapshot_digest": digest(manifest_bytes), "batch_dir": str(root.resolve())}
