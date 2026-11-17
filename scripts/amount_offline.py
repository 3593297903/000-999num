"""Release-evidence reader for amount-mode files, independent of the application.

This script imports neither the generator/checkers nor their reports.  It reads
each original record and the actual TXT, using a separately supplied original
input configuration.  It is not a fourth checker in the daily application.
"""
import argparse
from datetime import datetime
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid


MODE = "set_amount_small_v1"
VERSIONS = {
    "rules_version": "set_amount_small_rules_v1",
    "partition_version": "remaining_2_5_v1",
    "shuffle_version": "system_random_ids_v1",
    "format_version": "direct_amount_grouped_v2",
}
FILES = ("request.json", "group_plan.json", "items.jsonl", "groups.txt", "manifest.json")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: " + key)
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("nonstandard JSON constant: " + value)


def strict_json(data, *, canonical_required=False):
    require(not data.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM is forbidden")
    result = json.loads(data.decode("utf-8"), object_pairs_hook=unique_keys,
                        parse_constant=reject_constant)
    if canonical_required:
        require(canonical(result) == data, "noncanonical JSON bytes")
    return result


def money(value, max_digits, label):
    require(type(value) is str and 1 <= len(value) <= max_digits,
            label + ": money type/length")
    require(value[0] != "0" and all("0" <= char <= "9" for char in value),
            label + ": money is not canonical ASCII positive decimal")
    return int(value)


def bounded_read(path, bound):
    require(path.is_file() and not path.is_symlink() and not path.is_junction(),
            "missing or linked file: " + str(path))
    with path.open("rb") as stream:
        require(os.fstat(stream.fileno()).st_size <= bound, "file exceeds limit: " + path.name)
        data = stream.read(bound + 1)
    require(len(data) <= bound, "file grew beyond limit: " + path.name)
    return data


def read_fixed(root, raw_total):
    names = {path.name for path in root.iterdir()}
    require(names - {"audits"} == set(FILES), "actual fixed-file collection differs from five files")
    if "audits" in names:
        audit_dir = root / "audits"
        require(audit_dir.is_dir() and not audit_dir.is_symlink() and not audit_dir.is_junction(),
                "invalid audits directory")
    limits = {"request.json": 60000, "group_plan.json": raw_total * 30 + 1000,
              "items.jsonl": raw_total * 310, "groups.txt": raw_total * 155,
              "manifest.json": 4096}
    buffers = {}
    for name in FILES:
        data = bounded_read(root / name, limits[name])
        require(data and data.endswith(b"\n") and b"\r" not in data
                and not data.startswith(b"\xef\xbb\xbf"), "invalid encoding/newline: " + name)
        buffers[name] = data
    return buffers


def inspect_batch(batch_dir, config_path):
    started = time.monotonic()
    root = Path(batch_dir).resolve(strict=True)
    config_file = Path(config_path).resolve(strict=True)
    config_bytes = bounded_read(config_file, 60000)
    config = strict_json(config_bytes)
    require(type(config) is dict and set(config) == {"set_count", "amounts"}, "original config fields")
    count, source_strings = config["set_count"], config["amounts"]
    require(type(count) is int and 1 <= count <= 300, "original set count must be 1..300")
    require(type(source_strings) is list and len(source_strings) == count, "original amount row count")
    frozen = tuple(money(value, 100, "original config") for value in source_strings)
    raw_expected = 1000 * count
    per_code_expected = sum(frozen)
    whole_expected = 1000 * per_code_expected
    source_digits = max(map(len, source_strings))
    merged_digits = len(str(per_code_expected))
    buffers = read_fixed(root, raw_expected)
    fingerprints = {name: {"size": len(data), "sha256": sha256(data)} for name, data in buffers.items()}

    request = strict_json(buffers["request.json"], canonical_required=True)
    keys = {"schema_version", "mode", "batch_id", "set_count", "sets", "software_version", "created_at", *VERSIONS}
    require(type(request) is dict and set(request) == keys, "request field collection")
    require(type(request["schema_version"]) is int and request["schema_version"] == 2, "request schema")
    require(request["mode"] == MODE, "request mode")
    bid = request["batch_id"]
    require(type(bid) is str and 1 <= len(bid) <= 128
            and re.fullmatch(r"[A-Za-z0-9_-]+", bid, re.ASCII) is not None, "batch identity")
    for name, expected in VERSIONS.items():
        require(type(request[name]) is str and request[name] == expected, "unsupported " + name)
    require(type(request["software_version"]) is str and 1 <= len(request["software_version"]) <= 64,
            "software version field")
    require(type(request["created_at"]) is str and len(request["created_at"]) <= 64, "creation time field")
    timestamp = datetime.fromisoformat(request["created_at"])
    require(timestamp.tzinfo is not None and timestamp.utcoffset() is not None, "creation time has no timezone")
    require(type(request["set_count"]) is int and request["set_count"] == count, "request differs from original set count")
    requested_sets = request["sets"]
    require(type(requested_sets) is list and len(requested_sets) == count, "request amount row count")
    for set_no, row in enumerate(requested_sets, 1):
        require(type(row) is dict and set(row) == {"set_no", "amount"}, "request source fields")
        require(type(row["set_no"]) is int and row["set_no"] == set_no, "request source order")
        require(money(row["amount"], source_digits, "request") == frozen[set_no - 1],
                "request differs from independently supplied original amount")

    manifest = strict_json(buffers["manifest.json"], canonical_required=True)
    require(type(manifest) is dict and set(manifest) == {"schema_version", "mode", "batch_id", "files"}, "manifest fields")
    require(type(manifest["schema_version"]) is int and manifest["schema_version"] == 2
            and manifest["mode"] == MODE and manifest["batch_id"] == bid, "manifest identity/version")
    entries = manifest["files"]
    require(type(entries) is list and len(entries) == 4, "manifest must list four data files")
    seen_files = set()
    for entry in entries:
        require(type(entry) is dict and set(entry) == {"path", "size", "sha256"}, "manifest entry fields")
        name = entry["path"]
        require(type(name) is str and name in FILES[:4] and name not in seen_files, "manifest path collection")
        seen_files.add(name)
        require(type(entry["size"]) is int and entry["size"] == len(buffers[name]), "manifest size: " + name)
        require(type(entry["sha256"]) is str and entry["sha256"] == fingerprints[name]["sha256"],
                "manifest hash: " + name)

    plan = strict_json(buffers["group_plan.json"], canonical_required=True)
    require(type(plan) is dict and set(plan) == {"schema_version", "mode", "batch_id", "group_count", "groups"}, "plan fields")
    require(type(plan["schema_version"]) is int and plan["schema_version"] == 2
            and plan["mode"] == MODE and plan["batch_id"] == bid, "plan identity/version")
    group_count, groups = plan["group_count"], plan["groups"]
    require(type(group_count) is int and (raw_expected + 4) // 5 <= group_count <= raw_expected // 2, "group count bounds")
    require(type(groups) is list and len(groups) == group_count, "plan group list")
    sizes = []
    for group_no, row in enumerate(groups, 1):
        require(type(row) is dict and set(row) == {"group", "raw_count"}, "plan group fields")
        require(type(row["group"]) is int and row["group"] == group_no, "plan group sequence")
        q = row["raw_count"]
        require(type(q) is int and 2 <= q <= 5, "plan original group size")
        sizes.append(q)
    require(sum(sizes) == raw_expected, "plan raw total")

    ids = bytearray(raw_expected)
    cells = bytearray(raw_expected)
    raw_counts = [0] * 1000
    final_amounts = [0] * 1000
    raw_total = raw_amount_total = final_total = 0
    details = BytesIO(buffers["items.jsonl"])
    text = BytesIO(buffers["groups.txt"])
    rebuilt = BytesIO()
    group_checks = []
    separator = False
    text_limit = 29 + merged_digits
    title_limit = 7 + len(str(group_count))
    for group_no, q in enumerate(sizes, 1):
        expected_map, first_codes = {}, []
        for position in range(1, q + 1):
            line = details.readline(311)
            require(0 < len(line) <= 310 and line.endswith(b"\n"), "detail line absent/too long")
            row = strict_json(line, canonical_required=True)
            require(type(row) is dict and set(row) == {"group", "position", "item_id", "set_no", "code", "amount"}, "detail fields")
            require(type(row["group"]) is int and row["group"] == group_no, "detail group order")
            require(type(row["position"]) is int and row["position"] == position, "detail position order")
            item_id, set_no, code = row["item_id"], row["set_no"], row["code"]
            require(type(item_id) is int and 0 <= item_id < raw_expected, "item ID range/type")
            require(not ids[item_id], "duplicate item ID: " + str(item_id))
            require(type(set_no) is int and set_no == item_id // 1000 + 1, "ID/source binding")
            require(type(code) is str and len(code) == 3 and all("0" <= c <= "9" for c in code)
                    and int(code) == item_id % 1000, "ID/three-digit-code binding")
            amount = money(row["amount"], source_digits, "detail")
            require(amount == frozen[set_no - 1], "ID/source-amount binding")
            code_value = int(code)
            cell = (set_no - 1) * 1000 + code_value
            require(not cells[cell], f"duplicate source cell: {set_no}/{code}")
            ids[item_id] = cells[cell] = 1
            raw_counts[code_value] += 1
            raw_total += 1
            raw_amount_total += amount
            if code not in expected_map:
                first_codes.append(code)
                expected_map[code] = 0
            expected_map[code] += amount

        if group_no > 1:
            require(separator, "missing group separator")
        title_bytes = text.readline(title_limit + 1)
        require(len(title_bytes) <= title_limit, "TXT title length")
        title = title_bytes.decode("utf-8")
        require(title.startswith("第") and title.endswith("组\n"), "TXT title syntax")
        title_number = title[1:-2]
        require(title_number == str(group_no), "TXT group sequence")
        observed, line_amounts = {}, []
        observed_body = bytearray()
        last_amount = 0
        while True:
            line = text.readline(text_limit + 1)
            if line in (b"", b"\n"):
                separator = line == b"\n"
                break
            require(len(line) <= text_limit and line.endswith(b"\n"), "TXT body line length/newline")
            require(len(line_amounts) < 5, "TXT has more than five body lines")
            body = line.decode("utf-8")[:-1]
            require(body.count("直选") == 1 and body.endswith("元"), "TXT body separators")
            code_text, amount_text = body.split("直选")
            amount = money(amount_text[:-1], merged_digits, "TXT")
            require(last_amount < amount <= per_code_expected, "TXT amount row order/bound")
            last_amount = amount
            codes = code_text.split(".")
            require(1 <= len(codes) <= 5, "TXT code count per line")
            for code in codes:
                require(len(code) == 3 and all("0" <= c <= "9" for c in code), "TXT three-digit code")
                require(code not in observed, "TXT duplicate code in group")
                observed[code] = amount
                require(len(observed) <= 5, "TXT group has more than five distinct codes")
                final_amounts[int(code)] += amount
                final_total += amount
            line_amounts.append(amount)
            observed_body.extend(line)
        require(1 <= len(observed) <= 5 and line_amounts, "TXT group body is empty")
        require(observed == expected_map, f"group {group_no} code/amount map differs from original records")
        own_lines = []
        for amount in sorted(set(expected_map.values())):
            codes = [code for code in first_codes if expected_map[code] == amount]
            own_lines.append(".".join(codes) + "直选" + str(amount) + "元\n")
        body_bytes = "".join(own_lines).encode("utf-8")
        require(bytes(observed_body) == body_bytes, f"group {group_no} canonical body/order differs")
        rebuilt.write((("\n" if group_no > 1 else "") + f"第{group_no}组\n").encode("utf-8"))
        rebuilt.write(body_bytes)
        if group_no == group_count:
            require(not separator, "TXT must end in exactly one LF")
        else:
            require(separator, "TXT ends before planned groups")
        group_checks.append({"group": group_no, "raw_count": q, "distinct_codes": len(observed),
                             "map_equal": True, "canonical_body_equal": True})

    require(not details.read(1), "extra detail record after full pool")
    require(not text.read(1), "extra TXT content after final group")
    require(raw_total == raw_expected and all(value == 1 for value in ids), "full ID range not covered exactly once")
    require(all(value == 1 for value in cells), "source/code cells not all exactly once")
    require(rebuilt.getvalue() == buffers["groups.txt"], "full reconstructed TXT differs byte-for-byte")
    per_code = []
    for code_value in range(1000):
        require(raw_counts[code_value] == count, f"raw count differs for {code_value:03d}")
        require(final_amounts[code_value] == per_code_expected, f"final amount differs for {code_value:03d}")
        per_code.append({"code": f"{code_value:03d}", "expected_raw_count": count,
                         "actual_raw_count": raw_counts[code_value], "raw_difference": raw_counts[code_value] - count,
                         "expected_amount": str(per_code_expected), "actual_amount": str(final_amounts[code_value]),
                         "amount_difference": str(final_amounts[code_value] - per_code_expected)})
    require(raw_amount_total == whole_expected and final_total == whole_expected, "raw/final whole-pool amount")
    final_buffers = read_fixed(root, raw_expected)
    require(all(sha256(final_buffers[name]) == fingerprints[name]["sha256"] for name in FILES),
            "fixed files changed during offline reading")
    return {
        "status": "PASS", "evidence_kind": "independent_release_reader_not_daily_checker",
        "reader_version": "1.1.0", "mode": MODE, "batch_id": bid, "batch_dir": str(root),
        "original_config": str(config_file), "original_config_sha256": sha256(config_bytes),
        "reader_source_sha256": sha256(Path(__file__).read_bytes()),
        "fixed_file_count": len(fingerprints), "fixed_files": fingerprints,
        "set_count": count, "raw_total": raw_total, "group_count": len(group_checks),
        "per_code_expected_amount": str(per_code_expected), "total_amount": str(final_total),
        "actual_raw_total_amount": str(raw_amount_total), "checked_codes": len(per_code),
        "checked_source_code_cells": len(cells), "source_cell_count_sum": sum(cells),
        "unique_ids_checked": len(ids), "unique_ids_present": sum(ids),
        "id_range": [0, raw_expected - 1], "source_cells_exact": True, "unique_ids_exact": True,
        "all_group_maps_equal": True, "output_bytes_equal": True,
        "source_cell_counts": [list(cells[start:start + 1000]) for start in range(0, raw_expected, 1000)],
        "per_code_statistics": per_code, "group_checks": group_checks,
        "elapsed_seconds": time.monotonic() - started, "errors": [],
    }


def safe_receipt_path(value, batch_dir):
    """Read-only preflight for this QA receipt; the application has its own guard."""
    target = Path(value).resolve(strict=False)
    root = Path(batch_dir).resolve(strict=False)
    require(not target.is_relative_to(root), "receipt cannot be written inside the batch")
    for reserved in (Path("E:/number/data/batches"), Path("E:/number/data/staging"),
                     Path("E:/number/data_amount/batches"), Path("E:/number/data_amount/staging")):
        require(not target.is_relative_to(reserved.resolve(strict=False)), "receipt lies in a reserved data area")
    for ancestor in target.parent, *target.parent.parents:
        if ancestor.exists():
            require(ancestor.is_dir(), "receipt ancestor is not a directory")
        if (ancestor / "request.json").exists() and any((ancestor / name).exists() for name in ("manifest.json", "items.jsonl", "quotas.json")):
            raise ValueError("receipt lies inside a marked batch")
    require(not target.exists(), "receipt already exists; choose a new evidence file")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", required=True)
    parser.add_argument("--config", required=True, help="independently retained original input configuration")
    parser.add_argument("--result-json", required=True)
    args = parser.parse_args(argv)
    try:
        receipt = safe_receipt_path(args.result_json, args.batch_dir)
    except Exception as error:
        print("FAIL: " + str(error), file=sys.stderr)
        return 3
    try:
        result = inspect_batch(args.batch_dir, args.config)
        exit_code = 0
    except (Exception, KeyboardInterrupt) as error:
        result = {"status": "FAIL", "evidence_kind": "independent_release_reader_not_daily_checker",
                  "batch_dir": str(Path(args.batch_dir).resolve(strict=False)),
                  "original_config": str(Path(args.config).resolve(strict=False)),
                  "errors": [type(error).__name__ + ": " + str(error)]}
        exit_code = 3
    try:
        receipt.parent.mkdir(parents=True, exist_ok=True)
        pending = receipt.with_name(receipt.name + ".partial-" + uuid.uuid4().hex)
        with pending.open("xb") as stream:
            stream.write(canonical(result))
            stream.flush()
            os.fsync(stream.fileno())
        pending.rename(receipt)
    except Exception as error:
        print("FAIL: cannot publish receipt: " + str(error), file=sys.stderr)
        return 3
    print(json.dumps({"status": result["status"], "result_json": str(receipt),
                      "raw_total": result.get("raw_total"), "group_count": result.get("group_count"),
                      "errors": result["errors"]}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
