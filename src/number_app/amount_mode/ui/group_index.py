"""Bounded derived indices; selected groups always come from actual fixed bytes."""
from array import array
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
import logging
import os
import threading
import uuid

from ..limits import CACHE_MAX_BATCHES, CACHE_MAX_BYTES
from ..output_paths import protect_paths
from ..protocol import canonical_json, digest, read_snapshot, strict_json, synced_write

_CACHE = OrderedDict()
_CACHE_BYTES = 0
_CACHE_LOCK = threading.RLock()


class ResultDamagedError(ValueError):
    pass


def cache_info():
    with _CACHE_LOCK:
        return {"batches": len(_CACHE), "bytes": _CACHE_BYTES,
                "max_batches": CACHE_MAX_BATCHES, "max_bytes": CACHE_MAX_BYTES}


def clear_memory_cache():
    global _CACHE_BYTES
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE_BYTES = 0


def _signatures(root):
    result = []
    for name in ("request.json", "group_plan.json", "items.jsonl", "groups.txt", "manifest.json"):
        path = root / name
        if path.is_symlink() or path.is_junction() or not path.is_file():
            raise ResultDamagedError("固定文件缺失或被替换为链接：" + name)
        stat = path.stat()
        result.append((stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return tuple(result)


def _offsets(text, items, sizes):
    text_offsets = array("Q")
    cursor = 0
    for group in range(1, len(sizes) + 1):
        marker = f"第{group}组\n".encode("utf-8")
        if text[cursor:cursor + len(marker)] != marker:
            raise ResultDamagedError("实际TXT组标题不连续")
        text_offsets.append(cursor)
        end = text.find(b"\n\n", cursor)
        if group == len(sizes):
            if end != -1:
                raise ResultDamagedError("最后一组后有多余边界")
            cursor = len(text)
        else:
            if end == -1:
                raise ResultDamagedError("实际TXT组边界不完整")
            cursor = end + 2
    text_offsets.append(cursor)
    item_offsets = array("Q", [0])
    cursor = 0
    for size in sizes:
        for _ in range(size):
            end = items.find(b"\n", cursor)
            if end == -1:
                raise ResultDamagedError("原始明细边界不完整")
            cursor = end + 1
        item_offsets.append(cursor)
    if cursor != len(items):
        raise ResultDamagedError("明细存在额外记录")
    return text_offsets, item_offsets


def _money(text, maximum_digits):
    if type(text) is not str or not 1 <= len(text) <= maximum_digits or text[0] == "0" or any(ch < "0" or ch > "9" for ch in text):
        raise ResultDamagedError("金额不是规范正整数字符串")
    return int(text)


def _statistics(items, text, amounts):
    raw = [0] * 1000
    final = [0] * 1000
    for line in items.splitlines():
        row = strict_json(line)
        code = row["code"]
        if type(code) is not str or len(code) != 3 or not code.isascii() or not code.isdigit():
            raise ResultDamagedError("统计读取到非法号码")
        raw[int(code)] += 1
    digits = len(str(sum(amounts)))
    for block in text.decode("utf-8").rstrip("\n").split("\n\n"):
        for line in block.split("\n")[1:]:
            left, right = line.split("直选")
            if not right.endswith("元"):
                raise ResultDamagedError("统计读取到非法金额行")
            money = _money(right[:-1], digits)
            for code in left.split("."):
                if len(code) != 3 or not code.isascii() or not code.isdigit():
                    raise ResultDamagedError("统计读取到非法最终号码")
                final[int(code)] += money
    expected_raw, expected_amount = len(amounts), sum(amounts)
    return tuple({"code": f"{number:03d}", "expected_raw": expected_raw, "actual_raw": raw[number],
                  "raw_difference": raw[number] - expected_raw, "expected_amount": str(expected_amount),
                  "actual_amount": str(final[number]), "amount_difference": str(final[number] - expected_amount)}
                 for number in range(1000))


@dataclass
class _Entry:
    text: bytes
    items: bytes
    sizes: array
    text_offsets: array
    item_offsets: array
    signatures: tuple
    statistics: tuple

    @property
    def size_bytes(self):
        # Conservative allowance covers the 1000 statistics dictionaries/strings.
        return (len(self.text) + len(self.items) + len(self.sizes) * self.sizes.itemsize
                + (len(self.text_offsets) + len(self.item_offsets)) * 8 + 1024**2)


class ResultStore:
    def __init__(self, catalog, batch_id, cache_root=None):
        self.catalog = catalog
        self.batch_id = batch_id
        self.context = catalog.context(batch_id)
        self.batch_dir = catalog.path(batch_id)
        self.group_count = self.context.expected_group_count
        self.cache_root, = protect_paths(cache_root or catalog.root / "cache" / "amount-groups",
                                         workspace_roots=(catalog.root,))
        self.cache_file = self.cache_root / f"amount-index-{batch_id}-{self.context.expected_manifest_sha256}.json"
        self.cache_warning = None
        self._latest_audit_id = None
        self._report_expectations = ()
        self._key = (str(catalog.root), batch_id, self.context.expected_manifest_sha256)
        from number_app.storage.locking import WorkspaceLock
        from ..access import require_usable
        with WorkspaceLock(catalog.root):
            require_usable(catalog, batch_id)
            self._require_available()
            self._entry()

    def _require_available(self):
        row = self.catalog.get(self.batch_id)
        if (row is None or row["generation_status"] != "COMPLETED"
                or row["latest_verification_status"] != "PASS"):
            raise ValueError("只可取用已完成且最近完整复核通过的结果")
        if row["request_sha256"] != self.context.expected_request_sha256 or row["manifest_sha256"] != self.context.expected_manifest_sha256:
            raise ResultDamagedError("可信登记改变，停止取用")
        audit_id = row["latest_audit_id"]
        if audit_id != self._latest_audit_id:
            record = self.catalog.audit_record(audit_id)
            if record["status"] != "PASS" or record["batch_id"] != self.batch_id or len(record["checks"]) != 15:
                raise ResultDamagedError("最近会话索引不是完整通过结果")
            names = {f"{method}_{round_no}.json" for method in "ABC" for round_no in range(1, 6)}
            paths = []
            for check in record["checks"]:
                relative = check["report_relpath"]
                name = Path(relative).name if type(relative) is str else ""
                if name not in names or relative != f"audits/{audit_id}/{name}":
                    raise ResultDamagedError("最近会话报告索引路径不正确")
                names.remove(name)
                paths.append((self.batch_dir / relative, check["report_sha256"]))
            if names or record["summary_relpath"] != f"audits/{audit_id}/summary.json":
                raise ResultDamagedError("最近会话报告索引不齐全")
            paths.append((self.batch_dir / record["summary_relpath"], record["summary_sha256"]))
            self._report_expectations = tuple(paths)
            self._latest_audit_id = audit_id
        # Reports are small. Bind every take/copy to their original accepted
        # hashes without repeatedly reading a large fixed snapshot.
        for path, expected_hash in self._report_expectations:
            if (path.is_symlink() or path.is_junction() or path.resolve() != path
                    or not path.is_file() or path.stat().st_size > 65536
                    or digest(path.read_bytes()) != expected_hash):
                raise ResultDamagedError("最近核查报告缺失或指纹变化，停止取用")

    def _load_cached_offsets(self, buffers):
        path = self.cache_file
        if not path.exists():
            return None
        if path.is_symlink() or path.is_junction() or not path.is_file() or path.stat().st_size > CACHE_MAX_BYTES:
            return None
        try:
            value = strict_json(path.read_bytes())
            if (type(value) is not dict or value.get("cache_version") != 1
                    or value.get("batch_id") != self.batch_id
                    or value.get("snapshot_digest") != self.context.expected_manifest_sha256
                    or value.get("groups_sha256") != digest(buffers["groups.txt"])
                    or value.get("items_sha256") != digest(buffers["items.jsonl"])):
                return None
            result = []
            for key, data in (("text_offsets", buffers["groups.txt"]), ("item_offsets", buffers["items.jsonl"])):
                numbers = value[key]
                if (type(numbers) is not list or len(numbers) != self.group_count + 1
                        or any(type(n) is not int or not 0 <= n <= len(data) for n in numbers)
                        or numbers[-1] != len(data)):
                    return None
                result.append(array("Q", numbers))
            return tuple(result)
        except (ValueError, KeyError, OSError, TypeError):
            return None

    def _write_offsets(self, entry):
        temp = None
        owns_temp = False
        try:
            if self.cache_file.is_symlink() or self.cache_file.is_junction():
                raise ValueError("缓存文件不能为链接")
            temp = self.cache_file.with_name(self.cache_file.name + "." + uuid.uuid4().hex + ".tmp")
            final, temp = protect_paths(self.cache_file, temp, workspace_roots=(self.catalog.root,),
                                        file_paths=(self.cache_file, temp))
            self.cache_root.mkdir(parents=True, exist_ok=True)
            payload = {"cache_version": 1, "batch_id": self.batch_id,
                       "snapshot_digest": self.context.expected_manifest_sha256,
                       "groups_sha256": digest(entry.text), "items_sha256": digest(entry.items),
                       "text_offsets": list(entry.text_offsets), "item_offsets": list(entry.item_offsets)}
            with temp.open("xb") as stream:
                owns_temp = True
                stream.write(canonical_json(payload))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, final)
            owns_temp = False
            derived = sorted((p for p in self.cache_root.glob("amount-index-*.json")
                              if p.is_file() and not p.is_symlink() and not p.is_junction()
                              and p.resolve().parent == self.cache_root), key=lambda p: p.stat().st_mtime_ns, reverse=True)
            kept_bytes = 0
            for index, path in enumerate(derived):
                kept_bytes += path.stat().st_size
                if index >= CACHE_MAX_BATCHES or kept_bytes > CACHE_MAX_BYTES:
                    path.unlink()  # Only this program's derived files, never recursive.
        except (OSError, ValueError) as error:
            self.cache_warning = str(error)
            logging.getLogger(__name__).warning("分组缓存未保存：%s", error)
        finally:
            if owns_temp and temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    logging.getLogger(__name__).warning("未能清理本次派生缓存临时文件：%s", temp)

    def _load(self, *, force_offsets=False):
        buffers = read_snapshot(self.batch_dir, self.context)
        plan = strict_json(buffers["group_plan.json"])
        sizes = array("B", (row["raw_count"] for row in plan["groups"]))
        if len(sizes) != self.group_count or any(not 2 <= q <= 5 for q in sizes):
            raise ResultDamagedError("分组计划不合法")
        offsets = None if force_offsets else self._load_cached_offsets(buffers)
        rebuild = offsets is None
        if offsets is None:
            offsets = _offsets(buffers["groups.txt"], buffers["items.jsonl"], sizes)
        entry = _Entry(buffers["groups.txt"], buffers["items.jsonl"], sizes, *offsets,
                       _signatures(self.batch_dir), _statistics(buffers["items.jsonl"], buffers["groups.txt"], self.context.frozen_set_amounts))
        if force_offsets or rebuild:
            self._write_offsets(entry)
        return entry

    def _entry(self, *, force_offsets=False):
        global _CACHE_BYTES
        self._require_available()
        with _CACHE_LOCK:
            value = _CACHE.get(self._key)
            if value is not None and not force_offsets:
                _CACHE.move_to_end(self._key)
            elif value is not None:
                _CACHE_BYTES -= value.size_bytes
                del _CACHE[self._key]
                value = None
        if value is not None:
            if _signatures(self.batch_dir) != value.signatures:
                # Verify against the original anchor; never re-anchor changed files.
                read_snapshot(self.batch_dir, self.context)
                value.signatures = _signatures(self.batch_dir)
            return value
        # Parsing a large newly selected batch does not hold the global cache
        # mutex, keeping an already displayed batch responsive.
        value = self._load(force_offsets=force_offsets)
        with _CACHE_LOCK:
            if value.size_bytes > CACHE_MAX_BYTES:
                raise ValueError("单批缓存超过发布上限，未截断数据")
            old = _CACHE.pop(self._key, None)
            if old is not None:
                _CACHE_BYTES -= old.size_bytes
            while _CACHE and (len(_CACHE) >= CACHE_MAX_BATCHES or _CACHE_BYTES + value.size_bytes > CACHE_MAX_BYTES):
                _, old = _CACHE.popitem(last=False)
                _CACHE_BYTES -= old.size_bytes
            _CACHE[self._key] = value
            _CACHE_BYTES += value.size_bytes
            return value

    @staticmethod
    def _actual_slice(path, begin, end, expected):
        if begin >= end:
            raise ValueError("缓存偏移范围错误")
        with path.open("rb") as stream:
            stream.seek(begin)
            data = stream.read(end - begin)
        if data != expected:
            raise ResultDamagedError("实际分组文件字节已改变")
        return data

    def _group(self, group, entry):
        index = group - 1
        begin, end = entry.text_offsets[index:index + 2]
        segment = self._actual_slice(self.batch_dir / "groups.txt", begin, end, entry.text[begin:end])
        header, body = segment.split(b"\n", 1)
        if header != f"第{group}组".encode("utf-8"):
            raise ValueError("缓存指向另一组标题")
        if group < self.group_count:
            if not body.endswith(b"\n\n"):
                raise ValueError("缓存正文未到完整组边界")
            body = body[:-1]
        elif not body.endswith(b"\n") or body.endswith(b"\n\n") or end != len(entry.text):
            raise ValueError("最后一组缓存边界错误")
        if b"\n\n" in body:
            raise ValueError("缓存正文包含其他组边界")
        start, stop = entry.item_offsets[index:index + 2]
        raw = self._actual_slice(self.batch_dir / "items.jsonl", start, stop, entry.items[start:stop])
        records = [strict_json(line) for line in raw.splitlines()]
        if len(records) != entry.sizes[index] or any(row["group"] != group or row["position"] != p for p, row in enumerate(records, 1)):
            raise ValueError("明细缓存指向其他组或截断组")
        expected = {}
        first = []
        for row in records:
            amount = _money(row["amount"], 100)
            code = row["code"]
            if code not in expected:
                first.append(code)
            expected[code] = expected.get(code, 0) + amount
        parsed = {}
        previous = 0
        lines = body.decode("utf-8").splitlines()
        if not 1 <= len(lines) <= 5:
            raise ValueError("缓存正文不是完整小组")
        for line in lines:
            codes, amount_text = line.split("直选")
            if not amount_text.endswith("元"):
                raise ValueError("缓存正文金额尾部错误")
            amount = _money(amount_text[:-1], len(str(sum(self.context.frozen_set_amounts))))
            if amount <= previous:
                raise ValueError("缓存正文金额顺序错误")
            previous = amount
            code_list = codes.split(".")
            if code_list != [code for code in first if expected[code] == amount]:
                raise ValueError("缓存正文号码顺序或金额对应错误")
            for code in code_list:
                if code in parsed:
                    raise ValueError("缓存正文号码重复")
                parsed[code] = amount
        if parsed != expected:
            raise ValueError("缓存正文与原始来源不一致")
        return {"group": group, "body": body.decode("utf-8"), "raw_records": records,
                "raw_count": len(records), "distinct_count": len(parsed),
                "total_amount": str(sum(_money(r["amount"], 100) for r in records))}

    def get_group(self, group):
        if type(group) is not int or not 1 <= group <= self.group_count:
            raise ValueError("组号超出本批范围")
        entry = self._entry()
        try:
            return self._group(group, entry)
        except ResultDamagedError:
            raise
        except (ValueError, KeyError, IndexError, UnicodeError):
            # An apparently matching digest is insufficient. Rebuild from trusted
            # actual bytes and retry the requested group exactly once.
            return self._group(group, self._entry(force_offsets=True))

    def get_statistics(self):
        return [dict(row) for row in self._entry().statistics]
