"""Windows shared-log regression only; actual exe GUI is tested separately."""
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import time

from number_app.amount_mode.catalog import Catalog
from number_app.amount_mode.limits import LOG_MAX_BYTES, LOG_MAX_FILES
from number_app.amount_mode.runtime import configure_logging, log_event, prepare_runtime
from number_app.storage.locking import WorkspaceLock


def _fill(file):
    with file.open('wb') as stream:
        block = b'Q' * 1024**2
        for _ in range(10):
            stream.write(block)
        stream.flush(); os.fsync(stream.fileno())


def _history(root, result):
    command = [sys.executable, '-m', 'number_app.amount_mode', 'history', '--data-dir', str(root), '--result-json', str(result)]
    process = subprocess.run(command, capture_output=True, timeout=15)
    return process, command


def test_live_parent_logger_releases_log_and_cross_process_rotation_stays_bounded(tmp_path):
    root = tmp_path / 'data'
    with WorkspaceLock(root):
        Catalog(root)
    paths = prepare_runtime(root, tmp_path / 'backups')
    logger = configure_logging(paths)
    try:
        log_event(logger, {'event': 'logger_remains_alive_while_other_cli_rotates'})
        assert logger.handlers[0].stream is None
        log = paths['logs'] / 'amount.log'
        sentinel = paths['logs'] / 'unrelated-keep.txt'
        sentinel.write_bytes(b'derived rotation must not remove unrelated files')
        commands = []
        # Parent retains its configured handler throughout all ten real CLI
        # processes. Previously the first rotation raised WinError32.
        for index in range(10):
            _fill(log)
            process, command = _history(root, tmp_path / f'history-{index}.json')
            commands.append({'command': command, 'exit_code': process.returncode,
                             'stderr': process.stderr.decode('utf-8', errors='replace')})
            assert process.returncode == 0, process.stderr
            assert b'Logging error' not in process.stderr and b'WinError 32' not in process.stderr
            assert (paths['logs'] / 'amount.log.1').is_file()
            assert log.stat().st_size < LOG_MAX_BYTES
            assert logger.handlers[0].stream is None
        logs = list(paths['logs'].glob('amount.log*'))
        assert len(logs) == LOG_MAX_FILES
        assert all(path.stat().st_size <= LOG_MAX_BYTES for path in logs)
        assert sentinel.read_bytes() == b'derived rotation must not remove unrelated files'
        assert logger.handlers[0].failure_count == 0
        (tmp_path / 'shared-log-rotation.json').write_text(json.dumps({'commands': commands,
            'log_files': {p.name:p.stat().st_size for p in logs}, 'idle_stream_closed': True}, indent=2), encoding='utf-8')
    finally:
        for handler in logger.handlers[:]:
            handler.close(); logger.removeHandler(handler)


def test_log_lock_contention_is_bounded_diagnostic_and_not_business_failure(tmp_path):
    root = tmp_path / 'data'
    with WorkspaceLock(root):
        Catalog(root)
    paths = prepare_runtime(root, tmp_path / 'backups')
    logger = configure_logging(paths)
    log_event(logger, {'event': 'initial'})
    lock_file = paths['logs'] / '.amount-log.lock'
    try:
        with lock_file.open('r+b') as held:
            held.seek(0)
            msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                before = time.monotonic()
                process, _ = _history(root, tmp_path / 'contended-history.json')
                elapsed = time.monotonic() - before
            finally:
                held.seek(0)
                msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)
        assert process.returncode == 0
        assert json.loads((tmp_path / 'contended-history.json').read_text(encoding='utf-8'))['status'] == 'OK'
        assert elapsed < 3
        assert '诊断日志锁'.encode('utf-8') in process.stderr
        log_event(logger, {'event': 'after_contention'})
        assert logger.handlers[0].stream is None and logger.handlers[0].failure_count == 0
    finally:
        for handler in logger.handlers[:]:
            handler.close(); logger.removeHandler(handler)
