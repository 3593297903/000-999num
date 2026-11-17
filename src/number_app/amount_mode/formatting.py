"""Production formatter; the three checkers must not import this module."""
import re

from .limits import MAX_INPUT_DIGITS, MAX_SETS


def format_amount_group(records):
    if type(records) not in (list, tuple) or not 2 <= len(records) <= 5:
        raise ValueError("小组必须包含2～5条原始记录")
    amounts = {}
    seen_ids = set()
    for record in records:
        if type(record) is not dict:
            raise ValueError("原始记录必须为字典")
        code, amount = record.get("code"), record.get("amount")
        item_id, set_no = record.get("item_id"), record.get("set_no")
        if (type(code) is not str or re.fullmatch(r"[0-9]{3}", code, re.ASCII) is None
                or type(amount) is not int or not 1 <= amount < 10 ** MAX_INPUT_DIGITS
                or type(item_id) is not int or not 0 <= item_id < MAX_SETS * 1000
                or type(set_no) is not int or set_no != item_id // 1000 + 1
                or code != f"{item_id % 1000:03d}" or item_id in seen_ids):
            raise ValueError("原始记录编号、来源、三位号码或金额错误")
        seen_ids.add(item_id)
        amounts[code] = amounts.get(code, 0) + amount
    buckets = {}
    for code, amount in amounts.items():
        buckets.setdefault(amount, []).append(code)
    return "".join(".".join(buckets[amount]) + f"直选{amount}元\n" for amount in sorted(buckets))
