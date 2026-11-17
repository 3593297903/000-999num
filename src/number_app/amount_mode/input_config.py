"""Strict input/request protocol; not used for ABC business calculations."""
import json
import re
from datetime import datetime
from pathlib import Path

from . import __version__
from .limits import MAX_INPUT_DIGITS, MAX_SETS
from .protocol import MODE, SCHEMA_VERSION, VERSIONS, canonical_json
from .spec import AmountBatchSpec

RULES_VERSION = VERSIONS["rules_version"]
PARTITION_VERSION = VERSIONS["partition_version"]
SHUFFLE_VERSION = VERSIONS["shuffle_version"]
FORMAT_VERSION = VERSIONS["format_version"]
MAX_CONFIG_BYTES = 1024 + MAX_SETS * (MAX_INPUT_DIGITS + 16)
MAX_REQUEST_BYTES = 4096 + MAX_SETS * (MAX_INPUT_DIGITS + 64)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重复JSON键：{key}")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"不支持非标准JSON数值：{value}")


def load_json_strict(data, *, max_bytes=MAX_REQUEST_BYTES):
    if type(data) is str:
        data = data.encode("utf-8")
    if type(data) is not bytes or len(data) > max_bytes:
        raise ValueError("JSON文件超过支持范围或输入类型错误")
    if data.startswith(b"\xef\xbb\xbf"):
        raise ValueError("JSON不允许UTF-8 BOM")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("无效JSON：" + str(error)) from error


def parse_amount(value):
    if (type(value) is not str or not 1 <= len(value) <= MAX_INPUT_DIGITS
            or re.fullmatch(r"[1-9][0-9]*", value, flags=re.ASCII) is None):
        raise ValueError(f"金额必须为1～{MAX_INPUT_DIGITS}位ASCII正整数字符串")
    return int(value)


def parse_config(data):
    obj = load_json_strict(data, max_bytes=MAX_CONFIG_BYTES) if type(data) in (bytes, str) else data
    if type(obj) is not dict or set(obj) != {"set_count", "amounts"}:
        raise ValueError("配置必须且仅包含set_count和amounts")
    count, amounts = obj["set_count"], obj["amounts"]
    if type(count) is not int or not 1 <= count <= MAX_SETS:
        raise ValueError(f"套数必须为1～{MAX_SETS}的整数")
    if type(amounts) is not list or len(amounts) != count:
        raise ValueError("金额行数必须与套数一致")
    return AmountBatchSpec(tuple(parse_amount(value) for value in amounts))


def load_config(path):
    file = Path(path)
    if file.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError("输入配置文件超限")
    with file.open("rb") as stream:
        data = stream.read(MAX_CONFIG_BYTES + 1)
    return parse_config(data)


def _identity(batch_id, created_at, software_version):
    if type(batch_id) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", batch_id, re.ASCII) is None:
        raise ValueError("批次编号格式错误")
    if type(software_version) is not str or not 1 <= len(software_version) <= 64:
        raise ValueError("软件版本格式错误")
    if type(created_at) is not str or len(created_at) > 64:
        raise ValueError("创建时间必须为带时区的ISO 8601")
    try:
        timestamp = datetime.fromisoformat(created_at)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("创建时间缺少时区")
    except (ValueError, TypeError) as error:
        raise ValueError("创建时间必须为带时区的ISO 8601") from error


def build_request_bytes(spec, *, batch_id, created_at=None, software_version=__version__):
    if type(spec) is not AmountBatchSpec:
        raise ValueError("请求必须使用已冻结的AmountBatchSpec")
    if created_at is None:
        created_at = datetime.now().astimezone().isoformat()
    _identity(batch_id, created_at, software_version)
    return canonical_json({
        "schema_version": SCHEMA_VERSION, "mode": MODE, "batch_id": batch_id,
        "set_count": spec.set_count,
        "sets": [{"set_no": index, "amount": str(amount)} for index, amount in enumerate(spec.amounts, 1)],
        "rules_version": RULES_VERSION, "partition_version": PARTITION_VERSION,
        "shuffle_version": SHUFFLE_VERSION, "format_version": FORMAT_VERSION,
        "software_version": software_version, "created_at": created_at,
    })


def validate_request(request):
    if type(request) in (bytes, str):
        request = load_json_strict(request)
    required = {"schema_version", "mode", "batch_id", "set_count", "sets", "rules_version",
                "partition_version", "shuffle_version", "format_version", "software_version", "created_at"}
    if type(request) is not dict or set(request) != required:
        raise ValueError("请求字段集合不正确")
    if type(request["schema_version"]) is not int or request["schema_version"] != SCHEMA_VERSION:
        raise ValueError("不支持的请求协议")
    for key, expected in (("mode", MODE), ("rules_version", RULES_VERSION),
                          ("partition_version", PARTITION_VERSION), ("shuffle_version", SHUFFLE_VERSION),
                          ("format_version", FORMAT_VERSION)):
        if request[key] != expected:
            raise ValueError("不支持的请求版本：" + key)
    _identity(request["batch_id"], request["created_at"], request["software_version"])
    count, sets = request["set_count"], request["sets"]
    if type(count) is not int or not 1 <= count <= MAX_SETS or type(sets) is not list or len(sets) != count:
        raise ValueError("请求套数与金额表不一致")
    amounts = []
    for index, row in enumerate(sets, 1):
        if (type(row) is not dict or set(row) != {"set_no", "amount"}
                or type(row["set_no"]) is not int or row["set_no"] != index):
            raise ValueError("请求套号必须按1～S连续排列")
        amounts.append(parse_amount(row["amount"]))
    return AmountBatchSpec(tuple(amounts))
