"""Bounded diagnostic files, kept outside immutable business data."""
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import msvcrt
import os
import time
from contextlib import contextmanager

from .limits import LOG_MAX_BYTES, LOG_MAX_FILES
from .output_paths import protect_paths
from .protocol import canonical_json, synced_write


@contextmanager
def _diagnostic_log_lock(path, timeout=0.35):
    """A separate short-lived OS byte lock; never acquire the workspace lock."""
    stream = None
    locked = False
    try:
        stream = Path(path).open('a+b')
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b'0'); stream.flush()
        deadline = time.monotonic() + timeout
        delay = 0.005
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('诊断日志锁繁忙，已结束有界等待；本条日志写入失败') from error
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.05)
        yield
    finally:
        if stream is not None:
            try:
                if locked:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                stream.close()


class _SharedRotatingHandler(RotatingFileHandler):
    """Serialize open/rotate/write/close across GUI and CLI processes on Windows."""
    def __init__(self, file, lock_file):
        super().__init__(file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_MAX_FILES - 1,
                         encoding='utf-8', delay=True)
        self.lock_file = lock_file
        self.failure_count = 0
        self.last_error = None

    def shouldRollover(self, record):
        if self.stream is None:
            self.stream = self._open()
        # TextIO.tell is a byte position; count UTF-8 bytes for Chinese messages.
        size = len((self.format(record) + self.terminator).encode('utf-8'))
        return self.maxBytes > 0 and self.stream.tell() > 0 and self.stream.tell() + size >= self.maxBytes

    def emit(self, record):
        try:
            with _diagnostic_log_lock(self.lock_file):
                try:
                    super().emit(record)
                finally:
                    if self.stream is not None:
                        self.stream.close()
                        self.stream = None
        except Exception:
            # Diagnostic I/O must not change a completed business result into a
            # failed generation or trigger a second random generation.
            self.handleError(record)

    def handleError(self, record):
        import sys
        self.failure_count += 1
        self.last_error = str(sys.exc_info()[1])
        super().handleError(record)


def prepare_runtime(data_root, backup_root=None):
    root = Path(data_root)
    backup = Path(backup_root) if backup_root else (Path('E:/number/data_amount_backups') if root.resolve() == Path('E:/number/data_amount') else Path(str(root) + '_backups'))
    root, backup, logs, drafts, cache = protect_paths(root, backup, root / 'logs', root / 'drafts', root / 'cache', workspace_roots=(root,))
    return {'root': root, 'backup': backup, 'logs': logs, 'drafts': drafts, 'cache': cache}


def configure_logging(paths):
    file = paths['logs'] / 'amount.log'
    lock = paths['logs'] / '.amount-log.lock'
    file, lock = protect_paths(file, lock, workspace_roots=(paths['root'],), file_paths=(file, lock))
    paths['logs'].mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('amounttool')
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    handler = _SharedRotatingHandler(file, lock)
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def precheck_catalog(root):
    """Recognize an existing index before creating a lock, log or new file."""
    from .catalog import Catalog
    root = Path(root)
    if (root / 'history.sqlite').exists():
        Catalog(root)
    else:
        for name in ('batches', 'staging'):
            region = root / name
            if region.exists() and (not region.is_dir() or next(region.iterdir(), None) is not None):
                raise ValueError('索引缺失而业务文件存在，保留现场，请从可信包恢复')


def log_event(logger, event):
    if logger:
        logger.info(json.dumps(event, ensure_ascii=False, separators=(',', ':'), default=str))


def save_draft(paths, state):
    file = paths['drafts'] / 'input.json'
    temp = paths['drafts'] / 'input.tmp'
    file, temp = protect_paths(file, temp, workspace_roots=(paths['root'],), file_paths=(file, temp))
    paths['drafts'].mkdir(parents=True, exist_ok=True)
    synced_write(temp, canonical_json(state), exclusive=False)
    temp.replace(file)
