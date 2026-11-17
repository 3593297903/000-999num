"""One complete identity pool; source amount remains bound to its identity."""
from random import SystemRandom

from .limits import MAX_SETS


def shuffle_item_ids(total, rng=None):
    if type(total) is not int or not 1 <= total <= MAX_SETS * 1000:
        raise ValueError("编号池大小超出支持范围")
    ids = list(range(total))
    (rng if rng is not None else SystemRandom()).shuffle(ids)
    return ids


def iter_groups(ids, sizes):
    offset = 0
    for size in sizes:
        if type(size) is not int or not 2 <= size <= 5:
            raise ValueError("每组必须为2～5条原始记录")
        if offset + size > len(ids):
            raise ValueError("原始记录不足")
        yield ids[offset:offset + size]
        offset += size
    if offset != len(ids):
        raise ValueError("原始记录未全部分完")
