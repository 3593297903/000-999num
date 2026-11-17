"""C: exact ID bitmap and independent complete UTF-8 reconstruction."""
from datetime import datetime

from ..protocol import (MODE, VERSIONS, COMMON_PROOFS, read_snapshot, strict_json,
                        canonical_json, assert_snapshot_unchanged)


def _positive(value, max_digits):
    if type(value) is not str or not 1 <= len(value) <= max_digits or value[0] == "0":
        raise ValueError("C：金额不是有界规范正整数字符串")
    result = 0
    for char in value:
        digit = ord(char) - 48
        if digit < 0 or digit > 9:
            raise ValueError("C：金额含非ASCII数字")
        result = result * 10 + digit
    return result


def _integer(value, lower, upper, label):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError("C：" + label + "非法")
    return value


def check_c(batch_dir, context):
    data = read_snapshot(batch_dir, context)
    amounts = tuple(context.frozen_set_amounts)
    s = len(amounts)
    n = s * 1000
    t = 0
    for value in amounts:
        t += value
    m = 1000 * t
    request = strict_json(data["request.json"])
    fields = {"schema_version", "mode", "batch_id", "set_count", "sets", "software_version", "created_at", *VERSIONS}
    if type(request) is not dict or set(request) != fields or canonical_json(request) != data["request.json"]:
        raise ValueError("C：请求字段或JSON规范错误")
    if _integer(request["schema_version"], 2, 2, "协议") != 2 or request["mode"] != MODE or request["batch_id"] != context.batch_id:
        raise ValueError("C：请求身份错误")
    if any(request[key] != value for key, value in VERSIONS.items()) or type(request["software_version"]) is not str or not 1 <= len(request["software_version"]) <= 64:
        raise ValueError("C：不支持的版本")
    if type(request["created_at"]) is not str or len(request["created_at"]) > 64 or datetime.fromisoformat(request["created_at"]).utcoffset() is None:
        raise ValueError("C：创建时间没有时区")
    _integer(request["set_count"], s, s, "套数")
    if type(request["sets"]) is not list or len(request["sets"]) != s:
        raise ValueError("C：金额表长度不符")
    for index, row in enumerate(request["sets"], 1):
        if type(row) is not dict or set(row) != {"set_no", "amount"}:
            raise ValueError("C：来源金额表字段错误")
        _integer(row["set_no"], index, index, "来源套号")
        if _positive(row["amount"], 100) != amounts[index - 1]:
            raise ValueError("C：请求不等于最初冻结输入")
    plan = strict_json(data["group_plan.json"])
    if type(plan) is not dict or set(plan) != {"schema_version", "mode", "batch_id", "group_count", "groups"} or canonical_json(plan) != data["group_plan.json"]:
        raise ValueError("C：计划字段/JSON错误")
    _integer(plan["schema_version"], 2, 2, "计划协议")
    if plan["mode"] != MODE or plan["batch_id"] != context.batch_id:
        raise ValueError("C：计划身份错误")
    k = _integer(plan["group_count"], context.expected_group_count, context.expected_group_count, "计划组数")
    if type(plan["groups"]) is not list or len(plan["groups"]) != k:
        raise ValueError("C：计划组长度错误")
    sizes = []
    for group, row in enumerate(plan["groups"], 1):
        if type(row) is not dict or set(row) != {"group", "raw_count"}:
            raise ValueError("C：计划小组字段错误")
        _integer(row["group"], group, group, "计划组号")
        sizes.append(_integer(row["raw_count"], 2, 5, "原始条数"))
    if sum(sizes) != n:
        raise ValueError("C：计划合计不是完整池")
    lines = data["items.jsonl"].splitlines(keepends=True)
    if len(lines) != n:
        raise ValueError("C：明细不是完整原始条数")
    bitmap = bytearray(n)
    raw_counts = [0] * 1000
    final_amounts = [0] * 1000
    rebuilt = bytearray()
    cursor = 0
    actual_money = 0
    actual_groups = 0
    for group, count in enumerate(sizes, 1):
        first = []
        money = {}
        if group > 1:
            rebuilt.extend(b"\n")
        rebuilt.extend(("第" + str(group) + "组\n").encode("utf-8"))
        for position in range(1, count + 1):
            line = lines[cursor]
            cursor += 1
            if len(line) > 310 or not line.endswith(b"\n"):
                raise ValueError("C：明细行超过限额或不完整")
            row = strict_json(line)
            if type(row) is not dict or set(row) != {"group", "position", "item_id", "set_no", "code", "amount"} or canonical_json(row) != line:
                raise ValueError("C：明细行字段或JSON不规范")
            _integer(row["group"], group, group, "明细组号")
            _integer(row["position"], position, position, "组内位置")
            identity = _integer(row["item_id"], 0, n - 1, "唯一编号")
            if bitmap[identity]:
                raise ValueError(f"C：unique_ids_exact失败，重复ID {identity}")
            bitmap[identity] = 1
            source = identity // 1000 + 1
            code = identity % 1000
            _integer(row["set_no"], source, source, "编号对应套号")
            label = str(code).zfill(3)
            if type(row["code"]) is not str or row["code"] != label:
                raise ValueError("C：编号与号码不符")
            value = _positive(row["amount"], 100)
            if value != amounts[source - 1]:
                raise ValueError("C：来源金额不符")
            raw_counts[code] += 1
            if label not in money:
                first.append(label)
                money[label] = 0
            money[label] += value
            actual_money += value
        for value in sorted(set(money.values())):
            labels = []
            for label in first:
                if money[label] == value:
                    labels.append(label)
                    final_amounts[int(label)] += value
            rebuilt.extend((".".join(labels) + "直选" + str(value) + "元\n").encode("utf-8"))
        actual_groups += 1
    if not all(bitmap) or any(value != s for value in raw_counts):
        raise ValueError("C：完整ID或逐号原始份数失败")
    if bytes(rebuilt) != data["groups.txt"]:
        raise ValueError("C：output_bytes_equal失败，实际TXT与独立完整重建不同")
    # Only after the actual TXT bytes match may reconstruction prove final amounts.
    if any(value != t for value in final_amounts) or sum(final_amounts) != m or actual_money != m:
        raise ValueError("C：逐号金额或整池总金额错误")
    assert_snapshot_unchanged(batch_dir, context)
    return {"proofs": {key: True for key in (*COMMON_PROOFS, "unique_ids_exact", "output_bytes_equal")},
            "actual_raw_total": cursor, "actual_group_count": actual_groups,
            "actual_total_amount": str(sum(final_amounts)), "checked_codes": len(final_amounts),
            "checked_source_code_cells": sum(bitmap), "files_read": len(data)}
