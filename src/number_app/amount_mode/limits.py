"""Published, bounded support and preflight checks (no output side effects)."""
import ctypes
import shutil
from pathlib import Path

MAX_SETS = 300
MAX_INPUT_DIGITS = 100
CACHE_MAX_BATCHES = 3
CACHE_MAX_BYTES = 128 * 1024**2
LOG_MAX_FILES = 10
LOG_MAX_BYTES = 10 * 1024**2
CHECK_TIMEOUT_SECONDS = 300
RESOURCE_THRESHOLDS = {
    "gui_growth_after_warmup_bytes": 192 * 1024**2,
    "gui_handle_growth_after_warmup": 40,
    "idle_task_children": 0,
    "task_peak_rss_bytes": 1536 * 1024**2,
    "history_page_seconds": 2,
    "gui_response_seconds": 2,
}


def available_memory():
    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in
            ("total_phys", "avail_phys", "total_page", "avail_page", "total_virtual", "avail_virtual", "reserved")]
    value = Status()
    value.length = ctypes.sizeof(value)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
        raise OSError("无法读取可用内存，已停止")
    return value.avail_phys


def resource_preflight(spec, data_root, backup_root):
    """Check both volumes before allocating a pool or creating a batch."""
    rows = spec.raw_total
    # JSON detail, plan, text, a full backup and temporary copies; includes headroom.
    estimated = max(64 * 1024**2, rows * (700 + 3 * MAX_INPUT_DIGITS))
    memory = max(128 * 1024**2, rows * 900)
    if available_memory() < memory:
        raise OSError(f"可用内存不足：本批需要至少{memory // 1024**2}MiB余量")
    for value in (data_root, backup_root):
        current = Path(value).resolve()
        while not current.exists():
            if current.parent == current:
                raise OSError('磁盘不可用或未连接：' + str(current))
            current = current.parent
        if shutil.disk_usage(current).free < estimated:
            raise OSError(f"磁盘空间不足：{current}需要至少{estimated // 1024**2}MiB余量")
    return {"estimated_volume_bytes": estimated, "required_memory_bytes": memory}
