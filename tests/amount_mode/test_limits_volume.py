import ctypes
import time
import pytest
from number_app.amount_mode.spec import AmountBatchSpec
from number_app.amount_mode.limits import resource_preflight


def test_unavailable_backup_drive_is_bounded_readonly_rejection(tmp_path):
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    free = next((chr(index + 65) + ':/' for index in range(25, 3, -1) if not mask & (1 << index)), None)
    if free is None:
        pytest.skip('所有候选盘符均已连接，不能模拟不存在的本地卷')
    before = list(tmp_path.iterdir())
    started = time.monotonic()
    with pytest.raises(OSError, match='磁盘不可用或未连接'):
        resource_preflight(AmountBatchSpec((1,)), tmp_path, free + 'amount-qa-unavailable')
    assert time.monotonic() - started < 2
    assert list(tmp_path.iterdir()) == before
