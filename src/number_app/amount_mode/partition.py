"""Random original-record group sizes, with no one-record remainder."""
from random import SystemRandom

from .limits import MAX_SETS


def legal_group_sizes(remaining):
    if type(remaining) is not int or not 2 <= remaining <= MAX_SETS * 1000:
        raise ValueError("剩余条数超出支持范围")
    return tuple(size for size in (2, 3, 4, 5)
                 if size <= remaining and (remaining - size == 0 or remaining - size >= 2))


def generate_group_sizes(total, rng=None):
    if type(total) is not int or not 2 <= total <= MAX_SETS * 1000:
        raise ValueError("总条数必须为2～300000的整数")
    source = rng if rng is not None else SystemRandom()
    remaining, sizes = total, []
    while remaining:
        allowed = legal_group_sizes(remaining)
        size = source.choice(allowed)
        if type(size) is not int or size not in allowed:
            raise ValueError("随机源返回了非法组大小")
        sizes.append(size)
        remaining -= size
    source.shuffle(sizes)
    if sum(sizes) != total or any(type(q) is not int or not 2 <= q <= 5 for q in sizes):
        raise ValueError("分组结果不完整")
    return sizes
