"""B: complete source records sorted against a standard; separate TXT state parser.

No generator, formatter, other checker or shared monetary business logic is used.
"""
from datetime import datetime

from ..protocol import (MODE, VERSIONS, COMMON_PROOFS, canonical_json,
                        strict_json, read_snapshot, assert_snapshot_unchanged)


def _money_b(value, digits):
    if type(value) is not str or not 1 <= len(value) <= digits:
        raise ValueError("B 金额类型或长度不合法")
    if value[0] == "0" or any(char < "0" or char > "9" for char in value):
        raise ValueError("B 金额不是规范ASCII正整数")
    return int(value)


def _integer_b(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("B " + label + "必须为范围内严格整数")
    return value


def _json_b(data, label):
    result = strict_json(data)
    if canonical_json(result) != data:
        raise ValueError("B " + label + "不是规范JSON")
    return result


def check_b(batch_dir, context):
    frozen = context.frozen_set_amounts
    if type(frozen) is not tuple or not 1 <= len(frozen) <= 300:
        raise ValueError("B 可信金额快照不合法")
    total_each = 0
    for amount in frozen:
        if type(amount) is not int or amount < 1 or len(str(amount)) > 100:
            raise ValueError("B 冻结金额必须为严格正整数")
        total_each += amount
    sets = len(frozen)
    expected_n = sets * 1000
    expected_money = total_each * 1000
    buffers = read_snapshot(batch_dir, context)
    request = _json_b(buffers["request.json"], "request")
    request_keys = {"schema_version", "mode", "batch_id", "set_count", "sets",
                    "rules_version", "partition_version", "shuffle_version", "format_version",
                    "software_version", "created_at"}
    if type(request) is not dict or set(request) != request_keys:
        raise ValueError("B 请求字段集合错误")
    if (_integer_b(request["schema_version"], 2, 2, "协议版本") != 2
            or request["mode"] != MODE or request["batch_id"] != context.batch_id):
        raise ValueError("B 请求身份或模式错误")
    for key, version in VERSIONS.items():
        if request[key] != version:
            raise ValueError("B 未知请求版本：" + key)
    if type(request["software_version"]) is not str or not 1 <= len(request["software_version"]) <= 64:
        raise ValueError("B 软件版本字段非法")
    stamp = request["created_at"]
    if type(stamp) is not str or len(stamp) > 64:
        raise ValueError("B 创建时间格式错误")
    when = datetime.fromisoformat(stamp)
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("B 创建时间必须包含时区")
    if _integer_b(request["set_count"], 1, 300, "套数") != sets:
        raise ValueError("B 请求套数与可信输入不一致")
    amount_rows = request["sets"]
    if type(amount_rows) is not list or len(amount_rows) != sets:
        raise ValueError("B 请求金额表长度不正确")
    for i, row in enumerate(amount_rows, 1):
        if type(row) is not dict or set(row) != {"set_no", "amount"}:
            raise ValueError("B 请求套字段错误")
        if _integer_b(row["set_no"], 1, sets, "来源套号") != i:
            raise ValueError("B 请求套号未按顺序连续排列")
        if _money_b(row["amount"], 100) != frozen[i - 1]:
            raise ValueError("B 请求金额与原始输入不一致")

    plan = _json_b(buffers["group_plan.json"], "group_plan")
    if type(plan) is not dict or set(plan) != {"schema_version", "mode", "batch_id", "group_count", "groups"}:
        raise ValueError("B 计划字段集合错误")
    _integer_b(plan["schema_version"], 2, 2, "计划协议")
    if plan["mode"] != MODE or plan["batch_id"] != context.batch_id:
        raise ValueError("B 计划身份不匹配")
    count = _integer_b(plan["group_count"], (expected_n + 4) // 5, expected_n // 2, "组数")
    if count != context.expected_group_count or type(plan["groups"]) is not list or len(plan["groups"]) != count:
        raise ValueError("B 计划组数与可信登记不一致")
    sizes = []
    for number, entry in enumerate(plan["groups"], 1):
        if type(entry) is not dict or set(entry) != {"group", "raw_count"}:
            raise ValueError("B 组计划字段错误")
        if _integer_b(entry["group"], 1, count, "计划组号") != number:
            raise ValueError("B 计划组号不连续")
        sizes.append(_integer_b(entry["raw_count"], 2, 5, "原始组大小"))
    if sum(sizes) != expected_n:
        raise ValueError("B 计划原始数量不等于完整池")

    # Build the independent standard from the immutable user input, never request declarations.
    standard = [(identity, identity // 1000 + 1, f"{identity % 1000:03d}", frozen[identity // 1000])
                for identity in range(expected_n)]
    actual = []
    cells = bytearray(expected_n)
    raw_per_code = [0] * 1000
    raw_money = 0
    group_amounts = [{} for _ in sizes]
    first_orders = [[] for _ in sizes]
    current_group = 1
    position = 0
    lines = buffers["items.jsonl"].splitlines(keepends=True)
    if len(lines) != expected_n:
        raise ValueError("B 原始明细条数错误")
    for line_number, raw_line in enumerate(lines, 1):
        if len(raw_line) > 310:
            raise ValueError(f"B 第{line_number}条明细超过行长限制")
        row = _json_b(raw_line, "items第" + str(line_number) + "行")
        if type(row) is not dict or set(row) != {"group", "position", "item_id", "set_no", "code", "amount"}:
            raise ValueError("B 明细字段集合错误")
        position += 1
        if position > sizes[current_group - 1]:
            current_group += 1
            position = 1
        if _integer_b(row["group"], 1, count, "明细组号") != current_group:
            raise ValueError("B 明细组号与物理顺序不符")
        if _integer_b(row["position"], 1, 5, "组内位置") != position:
            raise ValueError("B 明细组内位置不连续")
        identity = _integer_b(row["item_id"], 0, expected_n - 1, "唯一编号")
        set_no = _integer_b(row["set_no"], 1, sets, "来源套号")
        code = row["code"]
        if type(code) is not str or len(code) != 3 or any(ch < "0" or ch > "9" for ch in code):
            raise ValueError("B 号码必须为三位ASCII数字")
        code_no = int(code)
        money = _money_b(row["amount"], 100)
        if identity != (set_no - 1) * 1000 + code_no or money != frozen[set_no - 1]:
            raise ValueError("B 编号、来源、号码或金额绑定错误")
        actual.append((identity, set_no, code, money))
        cell = (set_no - 1) * 1000 + code_no
        if cells[cell] != 0:
            raise ValueError(f"B 来源格重复：第{set_no}套{code}")
        cells[cell] = 1
        raw_per_code[code_no] += 1
        raw_money += money
        mapping = group_amounts[current_group - 1]
        if code not in mapping:
            first_orders[current_group - 1].append(code)
        mapping[code] = mapping.get(code, 0) + money
    actual.sort()
    if actual != standard:
        raise ValueError("B 实际完整来源记录与标准排序不相等")
    if any(value != 1 for value in cells) or any(value != sets for value in raw_per_code):
        raise ValueError("B 来源格或逐号原始数量错误")
    if current_group != count or position != sizes[-1] or raw_money != expected_money:
        raise ValueError("B 明细组边界或原始金额合计错误")
    expected_group_table = sorted((group + 1, code, amount)
                                  for group, mapping in enumerate(group_amounts)
                                  for code, amount in mapping.items())

    # Separate parser: exact block boundaries, then split amount delimiter and dot codes.
    text = buffers["groups.txt"].decode("utf-8")
    if not text.endswith("\n") or text.endswith("\n\n"):
        raise ValueError("B TXT末尾必须恰好一个LF")
    blocks = text[:-1].split("\n\n")
    if len(blocks) != count:
        raise ValueError("B TXT实际组数不正确")
    actual_table = []
    final_per_code = [0] * 1000
    final_money = 0
    money_digits = len(str(total_each))
    for group, block in enumerate(blocks, 1):
        lines = block.split("\n")
        if not lines or len(lines[0]) > len(str(count)) + 2 or lines[0] != f"第{group}组":
            raise ValueError(f"B TXT第{group}组标题错误")
        body = lines[1:]
        if not 1 <= len(body) <= 5:
            raise ValueError("B TXT每组正文必须1～5行")
        seen_codes = set()
        last_amount = 0
        for line in body:
            if len(line) > 22 + money_digits:
                raise ValueError("B TXT金额行超长")
            sections = line.split("直选")
            if len(sections) != 2 or not sections[1].endswith("元"):
                raise ValueError("B TXT金额行分隔符错误")
            amount = _money_b(sections[1][:-1], money_digits)
            if amount > total_each or amount <= last_amount:
                raise ValueError("B TXT金额必须严格数值升序且不超过每号全局金额")
            last_amount = amount
            codes = sections[0].split(".")
            if not 1 <= len(codes) <= 5:
                raise ValueError("B TXT一行号码数量错误")
            for code in codes:
                if len(code) != 3 or any(ch < "0" or ch > "9" for ch in code) or code in seen_codes:
                    raise ValueError("B TXT号码不规范或同组重复")
                seen_codes.add(code)
                number = int(code)
                final_per_code[number] += amount
                final_money += amount
                actual_table.append((group, code, amount))
            expected_order = [code for code in first_orders[group - 1] if group_amounts[group - 1][code] == amount]
            if codes != expected_order:
                raise ValueError("B TXT同金额号码未按本组首次出现顺序排列或逐组金额错误")
        if not 1 <= len(seen_codes) <= 5:
            raise ValueError("B TXT整组号码数量超过1～5")
    actual_table.sort()
    if actual_table != expected_group_table:
        raise ValueError("B TXT组号、号码、金额排序表与原始逐组聚合不相等")
    if any(amount != total_each for amount in final_per_code):
        raise ValueError("B 全部1000号码的最终累计金额不等于输入合计")
    if final_money != expected_money:
        raise ValueError("B 最终整池金额错误")
    assert_snapshot_unchanged(batch_dir, context)
    return {"proofs": {**{name: True for name in COMMON_PROOFS}, "standard_records_equal": True},
            "actual_raw_total": len(actual), "actual_group_count": len(blocks),
            "actual_total_amount": str(final_money), "checked_codes": len(final_per_code),
            "checked_source_code_cells": sum(cells), "files_read": len(buffers)}
