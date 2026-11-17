"""A: source-cell arrays and an independent, full-line TXT parser.

No generator, formatter, input model or other checker is used here.  The shared
protocol layer supplies only the exact hashed byte buffers and strict JSON I/O.
"""
from array import array
from datetime import datetime
from io import BytesIO
import re

from ..protocol import (
    COMMON_PROOFS, MODE, VERSIONS, canonical_json, read_snapshot, strict_json,
)


_DECIMAL = re.compile(r"[1-9][0-9]*", re.ASCII)
_CODE = re.compile(r"[0-9]{3}", re.ASCII)
_BODY = re.compile(r"([0-9]{3}(?:\.[0-9]{3}){0,4})直选([1-9][0-9]*)元\n", re.ASCII)


def _need(condition, message):
    if not condition:
        raise ValueError("A: " + message)


def _money(value, digits, where):
    # Bound the input before a regex scan or conversion to an arbitrary int.
    _need(type(value) is str and 1 <= len(value) <= digits, where + "金额长度或类型错误")
    _need(_DECIMAL.fullmatch(value) is not None, where + "金额不是规范ASCII正整数")
    return int(value)


def _json_buffer(data, where):
    value = strict_json(data)
    _need(canonical_json(value) == data, where + "不是规范JSON字节")
    return value


def check_a(batch_dir, context):
    """Return actual statistics only after all A conditions hold; otherwise fail."""
    amounts = context.frozen_set_amounts
    _need(type(amounts) is tuple and 1 <= len(amounts) <= 300, "冻结金额不是支持的不可变元组")
    for value in amounts:
        _need(type(value) is int and value >= 1 and len(str(value)) <= 100, "冻结金额不合法")
    set_count = len(amounts)
    raw_expected = set_count * 1000
    per_code_expected = sum(amounts)
    whole_expected = per_code_expected * 1000
    source_digits = max(len(str(value)) for value in amounts)
    merged_digits = len(str(per_code_expected))
    group_count = context.expected_group_count
    _need(type(group_count) is int and (raw_expected + 4) // 5 <= group_count <= raw_expected // 2,
          "可信组数不合法")

    buffers = read_snapshot(batch_dir, context)
    request = _json_buffer(buffers["request.json"], "request")
    request_keys = {"schema_version", "mode", "batch_id", "set_count", "sets",
                    "software_version", "created_at", *VERSIONS}
    _need(type(request) is dict and set(request) == request_keys, "request字段集合错误")
    _need(type(request["schema_version"]) is int and request["schema_version"] == 2,
          "request协议版本错误")
    _need(type(request["batch_id"]) is str and 1 <= len(request["batch_id"]) <= 128
          and re.fullmatch(r"[A-Za-z0-9_-]+", request["batch_id"], re.ASCII) is not None,
          "request批次编号格式错误")
    _need(request["mode"] == MODE and request["batch_id"] == context.batch_id,
          "request模式或批次身份错误")
    for name, expected in VERSIONS.items():
        _need(type(request[name]) is str and request[name] == expected, "不支持的" + name)
    _need(type(request["software_version"]) is str and 1 <= len(request["software_version"]) <= 64,
          "软件版本字段错误")
    _need(type(request["created_at"]) is str and len(request["created_at"]) <= 64, "创建时间字段错误")
    try:
        created = datetime.fromisoformat(request["created_at"])
    except ValueError as error:
        raise ValueError("A: 创建时间不是ISO8601") from error
    _need(created.tzinfo is not None and created.utcoffset() is not None, "创建时间缺少时区")
    _need(type(request["set_count"]) is int and request["set_count"] == set_count,
          "request_matches_frozen_input: 套数不同")
    source_rows = request["sets"]
    _need(type(source_rows) is list and len(source_rows) == set_count,
          "request_matches_frozen_input: 金额行数不同")
    for index, source in enumerate(source_rows, 1):
        _need(type(source) is dict and set(source) == {"set_no", "amount"}, "来源套字段错误")
        _need(type(source["set_no"]) is int and source["set_no"] == index,
              "来源套号必须严格按1～S排列")
        _need(_money(source["amount"], source_digits, f"第{index}套") == amounts[index - 1],
              "request_matches_frozen_input: 来源金额不同")

    plan = _json_buffer(buffers["group_plan.json"], "group_plan")
    _need(type(plan) is dict and set(plan) == {"schema_version", "mode", "batch_id", "group_count", "groups"},
          "计划字段集合错误")
    _need(type(plan["schema_version"]) is int and plan["schema_version"] == 2
          and plan["mode"] == MODE and plan["batch_id"] == context.batch_id, "计划身份或协议错误")
    _need(type(plan["group_count"]) is int and plan["group_count"] == group_count, "计划组数不一致")
    planned = plan["groups"]
    _need(type(planned) is list and len(planned) == group_count, "计划组列表长度不一致")
    sizes = []
    for group, entry in enumerate(planned, 1):
        _need(type(entry) is dict and set(entry) == {"group", "raw_count"}, "组计划字段错误")
        _need(type(entry["group"]) is int and entry["group"] == group, "计划组号不连续")
        size = entry["raw_count"]
        _need(type(size) is int and 2 <= size <= 5, f"第{group}组原始条数不在2～5")
        sizes.append(size)
    _need(sum(sizes) == raw_expected, "组计划原始条数总和错误")

    raw_counts = array("I", [0]) * 1000
    source_cells = array("I", [0]) * raw_expected
    final_amounts = [0] * 1000
    actual_raw_total = 0
    raw_amount_total = 0
    output_amount_total = 0
    actual_group_count = 0
    details = BytesIO(buffers["items.jsonl"])
    text = BytesIO(buffers["groups.txt"])
    separator_seen = False
    # A five-code line has at most 19 ASCII code/dot bytes, nine Chinese bytes,
    # the bounded amount and one LF.  Guard before parsing or int conversion.
    body_byte_limit = 29 + merged_digits
    header_byte_limit = 7 + len(str(group_count))

    for group, size in enumerate(sizes, 1):
        expected_map = {}
        for position in range(1, size + 1):
            line = details.readline(311)
            _need(0 < len(line) <= 310 and line.endswith(b"\n"), "原始明细行缺失或超过长度上限")
            record = _json_buffer(line, "原始明细")
            _need(type(record) is dict and set(record) == {"group", "position", "item_id", "set_no", "code", "amount"},
                  "原始明细字段集合错误")
            _need(type(record["group"]) is int and record["group"] == group, "原始明细组号不连续")
            _need(type(record["position"]) is int and record["position"] == position, "组内position不连续")
            item_id, set_no, code = record["item_id"], record["set_no"], record["code"]
            _need(type(item_id) is int and 0 <= item_id < raw_expected, "原始编号越界或类型错误")
            _need(type(set_no) is int and 1 <= set_no <= set_count and set_no == item_id // 1000 + 1,
                  "原始编号与来源套号不对应")
            _need(type(code) is str and len(code) == 3 and _CODE.fullmatch(code) is not None
                  and code == f"{item_id % 1000:03d}", "原始编号与三位号码不对应")
            amount = _money(record["amount"], source_digits, "原始记录")
            _need(amount == amounts[set_no - 1], "per_record_amount_exact: 原始记录金额与来源套不对应")
            code_value = int(code)
            cell = (set_no - 1) * 1000 + code_value
            source_cells[cell] += 1
            _need(source_cells[cell] == 1, f"source_code_cells_exact: 第{set_no}套{code}重复")
            raw_counts[code_value] += 1
            actual_raw_total += 1
            raw_amount_total += amount
            expected_map[code] = expected_map.get(code, 0) + amount

        if group > 1:
            _need(separator_seen, "组间缺少唯一空行")
        header = text.readline(header_byte_limit + 1)
        _need(len(header) <= header_byte_limit and header == f"第{group}组\n".encode("utf-8"),
              f"TXT第{group}组标题不正确")
        actual_group_count += 1
        observed = {}
        previous_amount = 0
        body_lines = 0
        separator_seen = False
        while True:
            line = text.readline(body_byte_limit + 1)
            if line in (b"", b"\n"):
                separator_seen = line == b"\n"
                break
            _need(len(line) <= body_byte_limit, "TXT正文行超过本批长度上限")
            body_lines += 1
            _need(body_lines <= 5, "TXT一组正文超过5行")
            match = _BODY.fullmatch(line.decode("utf-8"))
            _need(match is not None, "TXT正文整行格式错误")
            codes = match.group(1).split(".")
            amount = _money(match.group(2), merged_digits, "TXT")
            _need(previous_amount < amount <= per_code_expected, "TXT金额行非严格数值升序或超过每号上限")
            previous_amount = amount
            expected_order = [code for code, value in expected_map.items() if value == amount]
            _need(codes == expected_order, "group_code_amounts_exact: 同金额号码或原组首次顺序不同")
            for code in codes:
                _need(code not in observed, "TXT同组号码重复")
                observed[code] = amount
                _need(len(observed) <= 5, "TXT同组不同号码超过5个")
                final_amounts[int(code)] += amount
                output_amount_total += amount
        _need(1 <= body_lines <= 5 and 1 <= len(observed) <= 5, "TXT组正文为空或数量非法")
        _need(observed == expected_map, "group_code_amounts_exact: TXT逐号金额与本组原始明细不同")
        if group == group_count:
            _need(not separator_seen, "TXT末尾必须恰好一个LF")
        else:
            _need(separator_seen, "TXT提前结束或组间没有空行")

    _need(not details.read(1), "原始明细存在计划之外的额外记录")
    _need(not text.read(1), "TXT存在额外组或尾部内容")
    _need(actual_raw_total == raw_expected and actual_group_count == group_count, "实际条数或组数错误")
    checked_cells = 0
    for count in source_cells:
        _need(count == 1, "source_code_cells_exact: 来源格缺失或重复")
        checked_cells += 1
    checked_codes = 0
    for code_value in range(1000):
        _need(raw_counts[code_value] == set_count, f"raw_per_code_exact: {code_value:03d}原始份数错误")
        _need(final_amounts[code_value] == per_code_expected,
              f"final_per_code_amount_exact: {code_value:03d}累计金额错误")
        checked_codes += 1
    _need(raw_amount_total == whole_expected and output_amount_total == whole_expected,
          "final_total_amount_exact: 原始或最终整池金额错误")
    # Reconfirm the on-disk snapshot after parsing the original hashed buffers.
    read_snapshot(batch_dir, context)
    return {
        "proofs": {name: True for name in COMMON_PROOFS},
        "actual_raw_total": actual_raw_total,
        "actual_group_count": actual_group_count,
        "actual_total_amount": str(output_amount_total),
        "checked_codes": checked_codes,
        "checked_source_code_cells": checked_cells,
        "files_read": len(buffers),
    }
