"""Immutable, strict input for the independent set-amount mode."""
from dataclasses import dataclass

from .limits import MAX_INPUT_DIGITS, MAX_SETS


@dataclass(frozen=True, slots=True)
class AmountBatchSpec:
    amounts: tuple[int, ...]

    def __post_init__(self):
        if type(self.amounts) is not tuple:
            raise ValueError("后台金额必须为不可变整数元组")
        if not 1 <= len(self.amounts) <= MAX_SETS:
            raise ValueError(f"支持1～{MAX_SETS}套")
        ceiling = 10 ** MAX_INPUT_DIGITS
        for index, amount in enumerate(self.amounts, 1):
            if type(amount) is not int or not 1 <= amount < ceiling:
                raise ValueError(f"第{index}套金额必须为1～{MAX_INPUT_DIGITS}位正整数")

    @property
    def set_count(self):
        return len(self.amounts)

    @property
    def raw_total(self):
        return self.set_count * 1000

    @property
    def per_code_amount(self):
        return sum(self.amounts)

    @property
    def total_amount(self):
        return self.per_code_amount * 1000
