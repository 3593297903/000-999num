"""Per-batch trusted packages and two bounded, separately labelled index slots."""
import hashlib
import os
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath

from number_app.storage.locking import WorkspaceLock
from .audit import AuditOutcome
from .catalog import APPLICATION_ID, DATABASE_VERSION, Catalog
from .input_config import parse_amount, validate_request
from .limits import resource_preflight
from .output_paths import protect_paths
from .protocol import (AuditContext, CHECKER_VERSION, FIXED_FILES, MODE, canonical_json,
                       digest, read_snapshot, strict_json, synced_write)
from .reports import MAX_REPORT_BYTES, read_report, validate_audit_session, validate_summary

BACKUP_VERSION = 1
MAX_METADATA_BYTES = 32 * 1024**2
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{64}", re.ASCII)


def _now():
    return datetime.now().astimezone().isoformat()


def _identifier(value):
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise ValueError("备份身份格式错误")
    return value


def _sha(value):
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ValueError("备份指纹格式错误")
    return value


def _file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _read_json(path, limit=MAX_METADATA_BYTES):
    p = Path(path)
    if not p.is_file() or p.is_symlink() or p.is_junction():
        raise ValueError("备份元数据缺失或包含链接：" + str(p))
    with p.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("备份元数据超出资源上限")
    value = strict_json(data)
    if canonical_json(value) != data or type(value) is not dict:
        raise ValueError("备份元数据必须为规范JSON对象")
    return value, data


def _entry(root, relative):
    path = Path(root) / relative
    return {"path": relative, "size": path.stat().st_size, "sha256": _file_hash(path)}


def _safe_relative(value):
    if type(value) is not str or "\\" in value:
        raise ValueError("备份路径必须为受限POSIX相对路径")
    p = PurePosixPath(value)
    if p.is_absolute() or not p.parts or any(part in ("", ".", "..") for part in p.parts) or p.as_posix() != value or ":" in value:
        raise ValueError("备份路径越界或不规范")
    return value


def _files(root):
    """Enumerate a single package without following symlinks or junctions."""
    root = Path(root)
    result, directories = {}, set()
    def visit(folder):
        for path in folder.iterdir():
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or path.is_junction():
                raise ValueError("备份包不允许链接或目录联接：" + relative)
            if path.is_dir():
                directories.add(relative)
                visit(path)
            elif path.is_file():
                result[relative] = path
            else:
                raise ValueError("备份包含未知文件对象")
    visit(root)
    return result, directories


def _copy_verified(source, target, expected_sha256, expected_size):
    """Hash the exact copied bytes, then independently read back the target."""
    source, target = Path(source), Path(target)
    if source.is_symlink() or source.is_junction() or not source.is_file():
        raise ValueError("来源文件缺失或为链接")
    if source.stat().st_size != expected_size:
        raise ValueError("来源文件尺寸已变化")
    target.parent.mkdir(parents=True, exist_ok=True)
    h, count = hashlib.sha256(), 0
    with source.open("rb") as src, target.open("xb") as dst:
        while block := src.read(1024**2):
            count += len(block)
            if count > expected_size:
                raise ValueError("来源文件在复制时增长")
            h.update(block)
            dst.write(block)
        dst.flush()
        os.fsync(dst.fileno())
    if count != expected_size or h.hexdigest() != expected_sha256 or _file_hash(target) != expected_sha256:
        raise ValueError("副本与原可信指纹不同")


def _checks(record):
    checks = []
    seen = set()
    if type(record.get("checks")) is not list or len(record["checks"]) > 15:
        raise ValueError("审查索引项数错误")
    aid = _identifier(record["audit_id"])
    for stored in record["checks"]:
        if type(stored) is not dict:
            raise ValueError("审查索引格式错误")
        method, round_no = stored.get("method"), stored.get("round")
        pair = (method, round_no)
        if (type(method) is not str or method not in "ABC" or len(method) != 1
                or type(round_no) is not int or not 1 <= round_no <= 5 or pair in seen):
            raise ValueError("审查索引组合非法或重复")
        seen.add(pair)
        created = stored.get("process_created_filetime")
        if created is not None:
            if type(created) is not str or not 1 <= len(created) <= 30 or not created.isascii() or not created.isdecimal():
                raise ValueError("进程创建身份格式错误")
            created = int(created)
        relative = stored.get("report_relpath")
        expected = f"audits/{aid}/{method}_{round_no}.json"
        if relative not in (None, expected):
            raise ValueError("审查报告索引不是本会话受限相对路径")
        sha = stored.get("report_sha256")
        if sha is not None:
            _sha(sha)
            if relative != expected:
                raise ValueError("报告指纹缺少对应文件路径")
        checks.append({key: stored.get(key) for key in ("method", "round", "execution_id", "pid", "started_at", "ended_at", "exit_code")}
                      | {"process_created_filetime": created, "report_path": Path(relative).name if relative else None,
                         "report_sha256": sha})
    return checks


def _audit_files(batch, context, record):
    """Validate committed session indices; failed sessions remain failure evidence."""
    aid = _identifier(record.get("audit_id"))
    checks = _checks(record)
    for key, expected in (("batch_id", context.batch_id), ("checker_version", CHECKER_VERSION),
                          ("provenance", context.provenance), ("request_sha256", context.expected_request_sha256),
                          ("manifest_sha256", context.expected_manifest_sha256), ("group_count", context.expected_group_count)):
        if type(record.get(key)) is not type(expected) or record[key] != expected:
            raise ValueError("备份审查索引身份或原基准不同：" + key)
    if record.get("status") not in ("PASS", "FAIL", "ERROR", "TIMEOUT", "CANCELLED"):
        raise ValueError("备份审查状态未知")
    session = Path(batch) / "audits" / aid
    if not session.is_dir() or session.is_symlink() or session.is_junction():
        raise ValueError("已登记审查会话缺失或为链接")
    selected = []
    for check in checks:
        name, sha = check["report_path"], check["report_sha256"]
        if name is None:
            continue
        file = session / name
        if sha is None:
            if file.exists():
                raise ValueError("实际失败报告缺少原始登记指纹，不能伪称已验证备份")
            continue
        if not file.is_file() or file.is_symlink() or file.stat().st_size > MAX_REPORT_BYTES or _file_hash(file) != sha:
            raise ValueError("审查原始报告缺失或与登记指纹不符")
        selected.append(f"audits/{aid}/{name}")
    summary_sha = record.get("summary_sha256")
    if summary_sha is not None:
        _sha(summary_sha)
        if record.get("summary_relpath") != f"audits/{aid}/summary.json":
            raise ValueError("审查汇总路径不是本会话相对路径")
        summary, raw = _read_json(session / "summary.json", MAX_REPORT_BYTES)
        if digest(raw) != summary_sha or summary.get("status") != record["status"]:
            raise ValueError("审查汇总状态或原指纹不符")
        expected_context = context.with_audit(aid)
        for key, expected in (("batch_id", context.batch_id), ("audit_id", aid),
                              ("expected_request_sha256", context.expected_request_sha256),
                              ("expected_manifest_sha256", context.expected_manifest_sha256)):
            if summary.get(key) != expected:
                raise ValueError("审查汇总身份与备份锚点不符")
        if canonical_json(summary.get("checks")) != canonical_json(checks):
            raise ValueError("审查汇总执行记录与目录索引不同")
        errors = strict_json(record.get("errors_json", "null"))
        if errors != [str(e)[:2000] for e in summary.get("errors", [])[:10]]:
            raise ValueError("审查错误索引与原汇总不同")
        selected.append(f"audits/{aid}/summary.json")
        if record["status"] == "PASS":
            valid, problems = validate_audit_session(batch, expected_context, checks, session)
            if not valid:
                raise ValueError("备份PASS会话原始证据不完整：" + ";".join(problems[:5]))
            valid, problems = validate_summary(session, expected_context, checks, summary_sha)
            if not valid:
                raise ValueError("备份PASS汇总不正确：" + ";".join(problems[:5]))
    elif (session / "summary.json").exists() or record.get("summary_relpath") is not None:
        raise ValueError("汇总文件没有原始登记指纹")
    if (session / "summary.txt").exists():
        if (not (session / "summary.txt").is_file() or (session / "summary.txt").is_symlink()
                or (session / "summary.txt").stat().st_size > MAX_REPORT_BYTES):
            raise ValueError("审查文字报告不是普通文件")
        selected.append(f"audits/{aid}/summary.txt")
    actual_files, _ = _files(session)
    if set(actual_files) != {Path(relative).name for relative in selected}:
        raise ValueError("已登记审查会话存在孤立或未知文件")
    return selected, checks


def _validate_amount_backup(package_path):
    """Read-only full package validation. No Catalog, mkdir, DB or anchor update."""
    root = Path(package_path).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("备份包必须为目录")
    marker, _ = _read_json(root / "BACKUP_COMPLETE.json", 4096)
    manifest, manifest_bytes = _read_json(root / "backup_manifest.json")
    required_marker = {"backup_version", "mode", "batch_id", "backup_id", "status", "backup_manifest_sha256"}
    if set(marker) != required_marker or marker.get("status") != "COMPLETE":
        raise ValueError("备份尚未完整发布")
    for obj in (marker, manifest):
        if type(obj.get("backup_version")) is not int or obj["backup_version"] != BACKUP_VERSION or obj.get("mode") != MODE:
            raise ValueError("不支持的备份版本/模式")
        _identifier(obj.get("batch_id")); _identifier(obj.get("backup_id"))
    if (marker["batch_id"], marker["backup_id"]) != (manifest["batch_id"], manifest["backup_id"]):
        raise ValueError("备份包身份不一致")
    if _sha(marker["backup_manifest_sha256"]) != digest(manifest_bytes):
        raise ValueError("备份外层清单指纹不同")
    if set(manifest) != {"backup_version", "mode", "batch_id", "backup_id", "original_manifest_sha256", "files"}:
        raise ValueError("备份清单字段错误")
    entries, indexed = manifest["files"], {}
    if type(entries) is not list or not 7 <= len(entries) <= 100000:
        raise ValueError("备份文件集合超限或缺失")
    for entry in entries:
        if type(entry) is not dict or set(entry) != {"path", "size", "sha256"}:
            raise ValueError("备份文件索引字段错误")
        relative = _safe_relative(entry["path"])
        if relative in indexed or relative in ("backup_manifest.json", "BACKUP_COMPLETE.json"):
            raise ValueError("备份清单重复或自引用")
        if type(entry["size"]) is not int or not 0 < entry["size"] <= 1024**3:
            raise ValueError("备份文件尺寸非法")
        _sha(entry["sha256"])
        indexed[relative] = entry
    actual, directories = _files(root)
    if set(actual) != set(indexed) | {"backup_manifest.json", "BACKUP_COMPLETE.json"}:
        raise ValueError("备份实际文件集合与清单不同")
    expected_directories = {parent.as_posix() for name in actual for parent in PurePosixPath(name).parents if parent.as_posix() != "."}
    if directories != expected_directories:
        raise ValueError("备份包含未登记空目录")
    for relative, entry in indexed.items():
        file = actual[relative]
        if file.stat().st_size != entry["size"] or _file_hash(file) != entry["sha256"]:
            raise ValueError("备份文件字节、尺寸或指纹错误：" + relative)
    anchor, _ = _read_json(root / "input_anchor.json")
    record, _ = _read_json(root / "catalog_record.json")
    for obj in (anchor, record):
        for key, expected in (("backup_version", 1), ("mode", MODE), ("batch_id", marker["batch_id"]), ("backup_id", marker["backup_id"])):
            if type(obj.get(key)) is not type(expected) or obj[key] != expected:
                raise ValueError("锚点/目录索引与备份身份不一致")
    request = anchor.get("original_request")
    spec = validate_request(request)
    original_request = canonical_json(request)
    if (request["batch_id"] != marker["batch_id"] or digest(original_request) != _sha(anchor.get("expected_request_sha256"))
            or anchor.get("frozen_set_amounts") != [str(a) for a in spec.amounts]
            or anchor.get("provenance") != "local_anchored"):
        raise ValueError("备份原始输入锚点不一致或不是本地可信来源")
    manifest_sha = _sha(anchor.get("expected_manifest_sha256"))
    if manifest_sha != manifest.get("original_manifest_sha256"):
        raise ValueError("备份原manifest锚点不同")
    group_count = anchor.get("expected_group_count")
    if type(group_count) is not int or not (spec.raw_total + 4) // 5 <= group_count <= spec.raw_total // 2:
        raise ValueError("备份原组数错误")
    if (record.get("batch_relpath") != "batches/" + marker["batch_id"] or record.get("generation_status") != "COMPLETED"
            or record.get("latest_verification_status") != "PASS"):
        raise ValueError("备份目录记录不是已核验完成批次或包含源绝对路径")
    for key, expected in (("set_count", spec.set_count), ("raw_total", spec.raw_total), ("group_count", group_count),
                          ("per_code_expected_amount", str(spec.per_code_amount)), ("total_amount", str(spec.total_amount))):
        if type(record.get(key)) is not type(expected) or record[key] != expected:
            raise ValueError("备份目录摘要与原输入不一致：" + key)
    context = AuditContext(MODE, marker["batch_id"], uuid.uuid4().hex, CHECKER_VERSION, spec.amounts,
                           anchor["expected_request_sha256"], manifest_sha, group_count)
    buffers = read_snapshot(root / "batch", context)
    if buffers["request.json"] != original_request:
        raise ValueError("备份实际请求不同于原始锚点")
    artifacts = record.get("artifacts")
    if type(artifacts) is not list or len(artifacts) != 5 or {a.get("path") for a in artifacts if type(a) is dict} != set(FIXED_FILES):
        raise ValueError("备份原固定文件索引不完整")
    for artifact in artifacts:
        name = artifact["path"]
        if (set(artifact) != {"path", "size", "sha256"} or type(artifact["size"]) is not int
                or artifact["size"] != len(buffers[name]) or artifact["sha256"] != digest(buffers[name])):
            raise ValueError("备份实际固定文件与原登记指纹不同")
    records = record.get("audits")
    if type(records) is not list or not records:
        raise ValueError("备份未覆盖已提交审查会话")
    audit_ids, expected_files, normalized = set(), {"input_anchor.json", "catalog_record.json", *("batch/" + n for n in FIXED_FILES)}, []
    latest = None
    for audit in records:
        if type(audit) is not dict or audit.get("audit_id") in audit_ids:
            raise ValueError("备份审查会话重复或非法")
        selected, checks = _audit_files(root / "batch", context, audit)
        audit_ids.add(audit["audit_id"])
        expected_files.update("batch/" + path for path in selected)
        normalized.append((audit, checks))
        if audit["audit_id"] == record.get("latest_audit_id"):
            latest = audit
    if set(indexed) != expected_files or latest is None or latest["status"] != "PASS":
        raise ValueError("备份覆盖会话、最新结论或实际文件集合不正确")
    return {"root": root, "backup_id": marker["backup_id"], "batch_id": marker["batch_id"],
            "anchor": anchor, "record": record, "context": context, "spec": spec,
            "manifest": manifest, "backup_manifest_sha256": digest(manifest_bytes),
            "entries": indexed, "audit_records": normalized, "covered_audit_ids": sorted(audit_ids)}


def validate_amount_backup(package_path):
    """Read-only full package validation with malformed shapes rejected as data errors."""
    try:
        return _validate_amount_backup(package_path)
    except (KeyError, TypeError, AttributeError, RecursionError) as error:
        raise ValueError("备份元数据结构非法：" + str(error)) from error


def backup_amount_batch(data_root, batch_id, destination_root):
    _identifier(batch_id)
    backup_id = uuid.uuid4().hex
    source_root, destination = protect_paths(data_root, destination_root, workspace_roots=(data_root,))
    final = destination / "batches" / batch_id / backup_id
    temporary = final.with_name(".partial-" + backup_id)
    final, temporary = protect_paths(final, temporary, workspace_roots=(source_root,))
    if not (source_root / "history.sqlite").is_file():
        raise ValueError("备份来源缺少原可信历史索引，未创建数据库")
    catalog = Catalog(source_root)  # Existing DB recognition is read-only.
    with WorkspaceLock(source_root):
        row = catalog.get(batch_id)
        if row is None or row["generation_status"] != "COMPLETED" or row["latest_verification_status"] != "PASS":
            raise ValueError("只能备份当前已核验完成的本地批次")
        context = catalog.context(batch_id)
        batch = catalog.path(batch_id)
        read_snapshot(batch, context)
        latest = catalog.audit_record(row["latest_audit_id"])
        catalog._validated_session(batch_id, context.with_audit(latest["audit_id"]),
                                   catalog._checks_for_validation(latest), latest["summary_sha256"])
        anchor = catalog.get_orig_anchor(batch_id)
        resource_preflight(validate_request(anchor["original_request"]), source_root, destination)
        for old in catalog.backup_coverage(batch_id):
            if old["status"] != "COMPLETE" or old["manifest_sha256"] != context.expected_manifest_sha256 or not old["package_path"]:
                continue
            package = Path(old["package_path"]).resolve(strict=False)
            if not package.is_relative_to(destination / "batches" / batch_id):
                continue
            try:
                validated = validate_amount_backup(package)
                if validated["context"].expected_manifest_sha256 != context.expected_manifest_sha256 or validated["batch_id"] != batch_id:
                    continue
            except (ValueError, OSError):
                continue
            catalog.mark_backup_reused(batch_id, validated["backup_id"])
            return {"status": "REUSED", "package_path": str(package), "backup_id": validated["backup_id"],
                    "batch_id": batch_id, "covered_audit_ids": validated["covered_audit_ids"],
                    "latest_audit_covered": row["latest_audit_id"] in validated["covered_audit_ids"]}
        try:
            record = catalog.export_catalog_record(batch_id)
            anchor = dict(anchor, backup_id=backup_id)
            record = dict(record, backup_id=backup_id)
            source_entries = {name: _entry(batch, name) for name in FIXED_FILES}
            for audit in record["audits"]:
                selected, _ = _audit_files(batch, context, audit)
                for relative in selected:
                    source_entries[relative] = _entry(batch, relative)
            temporary.mkdir(parents=True, exist_ok=False)
            synced_write(temporary / "input_anchor.json", canonical_json(anchor))
            synced_write(temporary / "catalog_record.json", canonical_json(record))
            for relative, entry in source_entries.items():
                _copy_verified(batch / relative, temporary / "batch" / relative, entry["sha256"], entry["size"])
            read_snapshot(batch, context)
            # Confirm all selected source report bytes stayed fixed during copying.
            for relative, entry in source_entries.items():
                if (batch / relative).stat().st_size != entry["size"] or _file_hash(batch / relative) != entry["sha256"]:
                    raise ValueError("备份复制期间来源文件变化")
            entries = [_entry(temporary, name) for name in ("input_anchor.json", "catalog_record.json")]
            entries.extend(_entry(temporary, "batch/" + name) for name in source_entries)
            manifest = {"backup_version": 1, "mode": MODE, "batch_id": batch_id, "backup_id": backup_id,
                        "original_manifest_sha256": context.expected_manifest_sha256, "files": sorted(entries, key=lambda e: e["path"])}
            manifest_bytes = canonical_json(manifest)
            synced_write(temporary / "backup_manifest.json", manifest_bytes)
            marker = {"backup_version": 1, "mode": MODE, "batch_id": batch_id, "backup_id": backup_id,
                      "status": "COMPLETE", "backup_manifest_sha256": digest(manifest_bytes)}
            synced_write(temporary / "BACKUP_COMPLETE.json", canonical_json(marker))
            validated = validate_amount_backup(temporary)
            if final.exists():
                raise FileExistsError("备份目标已存在，拒绝覆盖")
            temporary.rename(final)
            validated = validate_amount_backup(final)
            catalog.record_backup(batch_id, backup_id, manifest_sha256=context.expected_manifest_sha256,
                                  audit_ids=validated["covered_audit_ids"], package_path=final)
            return {"status": "COMPLETE", "package_path": str(final), "backup_id": backup_id,
                    "batch_id": batch_id, "covered_audit_ids": validated["covered_audit_ids"],
                    "latest_audit_covered": True, "backup_manifest_sha256": digest(manifest_bytes)}
        except BaseException as error:
            catalog.record_backup(batch_id, backup_id, manifest_sha256=context.expected_manifest_sha256,
                                  audit_ids=[], package_path=temporary if temporary.exists() else None,
                                  status="FAILED", error=str(error))
            raise


def _snapshot_info(file):
    file = Path(file)
    if not file.is_file() or file.is_symlink() or file.is_junction():
        raise ValueError("索引快照文件缺失或为链接")
    connection = sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if (connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0] != DATABASE_VERSION
                or connection.execute("PRAGMA quick_check").fetchone()[0] != "ok"):
            raise ValueError("索引快照数据库版本或完整性错误")
        count = connection.execute("SELECT count(*) FROM batches").fetchone()[0]
    finally:
        connection.close()
    return {"size": file.stat().st_size, "sha256": _file_hash(file), "batch_count": count}


def _slot(root, name):
    value, _ = _read_json(root / "slots" / (name + ".json"), 4096)
    if (type(value.get("snapshot_version")) is not int or value["snapshot_version"] != 1
            or value.get("mode") != MODE or value.get("slot") != name):
        raise ValueError("索引槽位版本或身份错误")
    relative = value.get("snapshot_relative")
    if type(relative) is not str or re.fullmatch(r"snapshots/amt-index-[0-9a-f]{32}\.sqlite", relative) is None:
        raise ValueError("索引槽位路径越界或未知")
    info = _snapshot_info(root / relative)
    if any(type(value.get(key)) is not type(info[key]) or value[key] != info[key] for key in ("size", "sha256", "batch_count")):
        raise ValueError("索引槽位文件与已验证指纹不同")
    return value


def snapshot_amount_catalog(data_root, backup_root):
    source, destination = protect_paths(data_root, backup_root, workspace_roots=(data_root,))
    identifier = uuid.uuid4().hex
    root = destination / "catalog"
    snapshots, slots = root / "snapshots", root / "slots"
    file = snapshots / ("amt-index-" + identifier + ".sqlite")
    temporary = file.with_suffix(".sqlite.partial")
    pointers = [slots / (name + ".json") for name in "AB"]
    pointer_temp = slots / (".pointer-" + identifier + ".tmp")
    paths = [snapshots, slots, file, temporary, *pointers, pointer_temp]
    checked = protect_paths(*paths, workspace_roots=(source,), file_paths=paths[2:])
    if any(actual != planned for actual, planned in zip(checked, paths)):
        raise ValueError("索引派生目录/指针包含链接，禁止轮换")
    if not (source / "history.sqlite").is_file():
        raise ValueError("索引快照来源不存在，未创建空库")
    catalog = Catalog(source)
    with WorkspaceLock(source):
        valid, errors = {}, {}
        for name in "AB":
            if (slots / (name + ".json")).exists():
                try:
                    valid[name] = _slot(root, name)
                except (ValueError, OSError, sqlite3.Error) as error:
                    errors[name] = str(error)
        chosen = next((name for name in "AB" if name not in valid), None)
        if chosen is None:
            chosen = min("AB", key=lambda name: (slots / (name + ".json")).stat().st_mtime_ns)
        snapshots.mkdir(parents=True, exist_ok=True)
        slots.mkdir(parents=True, exist_ok=True)
        with temporary.open("xb"):
            pass
        target = sqlite3.connect(temporary, isolation_level=None)
        try:
            with catalog.connection() as original:
                original.backup(target)
        finally:
            target.close()
        info = _snapshot_info(temporary)
        # Windows _commit/FlushFileBuffers needs a writable descriptor, even
        # though the already-committed snapshot bytes are not being changed.
        with temporary.open("r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        temporary.rename(file)
        pointer = {"snapshot_version": 1, "mode": MODE, "slot": chosen,
                   "snapshot_relative": file.relative_to(root).as_posix(), **info, "created_at": _now()}
        synced_write(pointer_temp, canonical_json(pointer))
        os.replace(pointer_temp, slots / (chosen + ".json"))
        _slot(root, chosen)
        current, referenced = {}, set()
        for name in "AB":
            path = slots / (name + ".json")
            if path.exists():
                # Preserve even an invalid but recognizable pointer's file as evidence.
                raw, _ = _read_json(path, 4096)
                if type(raw.get("snapshot_relative")) is str:
                    referenced.add(raw["snapshot_relative"])
                try:
                    current[name] = _slot(root, name)
                except (ValueError, OSError, sqlite3.Error) as error:
                    errors[name] = str(error)
        if not current or (valid and not any(name != chosen and name in current for name in valid)):
            # When there was only one valid slot and it was chosen due to a
            # missing other slot, chosen cannot equal it; normal rotations keep one.
            raise ValueError("索引轮换未保留先前有效槽位")
        removed = []
        for old in snapshots.iterdir():
            if (re.fullmatch(r"amt-index-[0-9a-f]{32}\.sqlite", old.name)
                    and old.is_file() and not old.is_symlink() and not old.is_junction()
                    and old.relative_to(root).as_posix() not in referenced):
                try:
                    _snapshot_info(old)
                except (ValueError, OSError, sqlite3.Error):
                    continue
                old.unlink()
                removed.append(old.name)
        return {"status": "SNAPSHOT_COMPLETE", "kind": "history_index_only_not_batch_backup", "slot": chosen,
                "snapshot_path": str(file), "sha256": info["sha256"], "slots": current,
                "previous_slot_errors": errors, "removed_unreferenced_derived_files": removed}
