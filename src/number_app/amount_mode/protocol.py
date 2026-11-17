"""Read-only wire declarations and generic JSON/byte I/O; no amount calculations."""
import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

MODE = "set_amount_small_v1"
CHECKER_VERSION = "amount_abc_1.1.0"
SCHEMA_VERSION = 2
VERSIONS = {
    "rules_version": "set_amount_small_rules_v1",
    "partition_version": "remaining_2_5_v1",
    "shuffle_version": "system_random_ids_v1",
    "format_version": "direct_amount_grouped_v2",
}
DATA_FILES = ("request.json", "group_plan.json", "items.jsonl", "groups.txt")
FIXED_FILES = (*DATA_FILES, "manifest.json")
COMMON_PROOFS = (
    "request_matches_frozen_input", "snapshot_matches_trusted_hashes",
    "source_code_cells_exact", "raw_per_code_exact", "per_record_amount_exact",
    "group_sizes_exact", "group_code_amounts_exact", "final_per_code_amount_exact",
    "final_total_amount_exact", "final_format_canonical",
)
EXTRA_PROOFS = {"A": (), "B": ("standard_records_equal",),
                "C": ("unique_ids_exact", "output_bytes_equal")}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical_json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重复JSON键：{key}")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"非法JSON常量：{value}")


def strict_json(data):
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return json.loads(data, object_pairs_hook=_pairs, parse_constant=_constant)


def synced_write(path, data, *, exclusive=True):
    with Path(path).open("xb" if exclusive else "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


@dataclass(frozen=True)
class AuditContext:
    mode: str
    batch_id: str
    audit_id: str
    checker_version: str
    frozen_set_amounts: tuple[int, ...]
    expected_request_sha256: str
    expected_manifest_sha256: str
    expected_group_count: int
    provenance: str = "local_anchored"

    def with_audit(self, audit_id):
        return replace(self, audit_id=audit_id)


def read_snapshot(batch_dir, context):
    """Read and hash exactly the buffers handed to each independent checker."""
    root = Path(batch_dir).resolve(strict=True)
    names = {p.name for p in root.iterdir()}
    if names - {"audits"} != set(FIXED_FILES):
        raise ValueError("固定文件集合不正确")
    if "audits" in names:
        p = root / "audits"
        if not p.is_dir() or p.is_symlink() or p.is_junction():
            raise ValueError("审查目录不是本批独立目录")
    if context.mode != MODE or context.checker_version != CHECKER_VERSION:
        raise ValueError("不支持的模式或核查器版本")
    if context.provenance not in ("local_anchored", "imported_declaration_only"):
        raise ValueError("不支持的来源标识")
    amounts = context.frozen_set_amounts
    if type(amounts) is not tuple or not 1 <= len(amounts) <= 300 or any(type(x) is not int or x < 1 or len(str(x)) > 100 for x in amounts):
        raise ValueError("不合法的可信输入上下文")
    n = len(amounts) * 1000
    if type(context.expected_group_count) is not int or not (n + 4) // 5 <= context.expected_group_count <= n // 2:
        raise ValueError("不合法的可信组数")
    bounds = {"request.json": 60000, "group_plan.json": n * 30 + 1000,
              "items.jsonl": n * 310, "groups.txt": n * 155, "manifest.json": 4096}
    buffers = {}
    for name in FIXED_FILES:
        p = root / name
        if not p.is_file() or p.is_symlink() or p.is_junction():
            raise ValueError("固定文件缺失或为链接：" + name)
        with p.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size > bounds[name]:
                raise ValueError("文件超过本批支持上限：" + name)
            data = stream.read(bounds[name] + 1)
        if len(data) > bounds[name] or not data or data.startswith(b"\xef\xbb\xbf") or b"\r" in data or not data.endswith(b"\n"):
            raise ValueError("固定文件大小、编码或换行不合法：" + name)
        buffers[name] = data
    if digest(buffers["manifest.json"]) != context.expected_manifest_sha256:
        raise ValueError("manifest与首次可信指纹不同")
    if digest(buffers["request.json"]) != context.expected_request_sha256:
        raise ValueError("request与原始可信指纹不同")
    manifest = strict_json(buffers["manifest.json"])
    if canonical_json(manifest) != buffers["manifest.json"]:
        raise ValueError("manifest不是规范JSON")
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "mode", "batch_id", "files"}:
        raise ValueError("manifest字段不合法")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 2 or manifest["mode"] != MODE or manifest["batch_id"] != context.batch_id:
        raise ValueError("manifest身份或版本不匹配")
    entries = manifest["files"]
    if not isinstance(entries, list) or len(entries) != 4:
        raise ValueError("manifest必须准确列出四个数据文件")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise ValueError("manifest文件字段不正确")
        name = entry["path"]
        if type(name) is not str or name not in DATA_FILES or name in seen:
            raise ValueError("manifest路径重复、绝对、越界或未知")
        seen.add(name)
        if type(entry["size"]) is not int or entry["size"] != len(buffers[name]) or entry["sha256"] != digest(buffers[name]):
            raise ValueError("数据文件指纹不匹配：" + name)
    return buffers


def assert_snapshot_unchanged(batch_dir, context):
    read_snapshot(batch_dir, context)
