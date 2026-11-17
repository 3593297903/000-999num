"""Stable source/frozen entry points; output paths are checked before any writes."""
import argparse
from pathlib import Path
import signal
import sys
import threading
import uuid

from .artifacts import CancelledError
from .input_config import load_config
from .output_paths import protect_paths, UnsafeOutputPath
from .protocol import canonical_json, synced_write
from .runtime import prepare_runtime, configure_logging, precheck_catalog, log_event


def _parser():
    parser = argparse.ArgumentParser(description='按套金额随机小组 1.1')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('gui', 'generate', 'verify', 'recover', 'backup', 'export', 'restore', 'restore-many', 'snapshot', 'history', 'group', 'version-info', 'migrate-index', 'rollback-check'):
        sub = commands.add_parser(name)
        sub.add_argument('--data-dir', default='E:/number/data_amount')
        sub.add_argument('--result-json')
        sub.add_argument('--backup-dir')
        if name == 'generate': sub.add_argument('--config', required=True)
        if name == 'verify': sub.add_argument('--batch-dir', required=True)
        if name in ('recover', 'backup', 'export', 'group'): sub.add_argument('--batch-id', required=True)
        if name in ('backup', 'export'): sub.add_argument('--destination', required=True)
        if name == 'restore': sub.add_argument('--package', required=True)
        if name == 'restore-many': sub.add_argument('--package', nargs='+', required=True)
        if name == 'history':
            sub.add_argument('--offset', type=int, default=0); sub.add_argument('--limit', type=int, default=30)
        if name == 'group': sub.add_argument('--group', type=int, required=True)
        if name == 'rollback-check': sub.add_argument('--snapshot-file', required=True)
    return parser


def _print(value, *, error=False):
    stream = sys.stderr if error else sys.stdout
    if stream is not None:
        try: stream.write(canonical_json(value).decode('utf-8')); stream.flush()
        except (OSError, UnicodeError): pass


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if callable(getattr(stream, 'reconfigure', None)):
            stream.reconfigure(encoding='utf-8')
    args = _parser().parse_args(argv)
    if args.command == 'gui':
        from .ui.app import run_gui
        return run_gui(args.data_dir)
    result_path = Path(args.result_json) if args.result_json else None
    receipt_temp = result_path.with_name(result_path.name + '.' + uuid.uuid4().hex + '.partial') if result_path else None
    logger = None; paths = None; result = None; code = 4; phase = 'preflight'; current_bid = None
    cancelled = threading.Event()
    from .windows_console import install_cancel_handler
    release_console_handler = install_cancel_handler(cancelled)
    previous_signal = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: cancelled.set())
    try:
        paths = prepare_runtime(args.data_dir, args.backup_dir)
        chosen = [paths['root'], paths['backup']]
        if hasattr(args, 'destination'): chosen.append(Path(args.destination))
        files = []
        if result_path is not None:
            chosen.extend((result_path, receipt_temp)); files = [result_path, receipt_temp]
        resolved = protect_paths(*chosen, workspace_roots=(paths['root'],), file_paths=files)
        if result_path is not None: result_path, receipt_temp = resolved[-2:]
        if hasattr(args, 'destination'): args.destination = str(resolved[2])
        version_commands = ('version-info', 'migrate-index', 'rollback-check')
        if args.command not in ('restore', 'restore-many', *version_commands): precheck_catalog(paths['root'])
        # A restore must validate its package and empty target before creating a log.
        if args.command not in ('restore', 'restore-many', 'backup', 'export', *version_commands): logger = configure_logging(paths)
        log_event(logger, {'event': 'command_started', 'command': args.command})
        def progress(event):
            nonlocal current_bid
            current_bid = event.get('batch_id', current_bid)
            log_event(logger, {'event': 'job_progress', **event})
        phase = 'input'
        if args.command == 'generate':
            spec = load_config(args.config)
            phase = 'operation'
            from .service import generate_amount_batch
            result = generate_amount_batch(spec, paths['root'], backup_root=paths['backup'], cancel=cancelled.is_set, progress=progress)
        elif args.command == 'verify':
            phase = 'operation'
            from .recovery import verify_amount_batch
            result = verify_amount_batch(args.batch_dir, paths['root'], cancel=cancelled.is_set, progress=progress)
        elif args.command == 'recover':
            phase = 'operation'
            from .recovery import recover_amount_batch
            result = recover_amount_batch(paths['root'], args.batch_id, cancel=cancelled.is_set, progress=progress, backup_root=paths['backup'])
        elif args.command == 'backup':
            phase = 'operation'
            from .backup import backup_amount_batch
            result = backup_amount_batch(paths['root'], args.batch_id, args.destination)
        elif args.command == 'export':
            phase = 'operation'
            from .export import export_amount_batch
            result = export_amount_batch(paths['root'], args.batch_id, args.destination)
        elif args.command == 'restore':
            phase = 'operation'
            from .restore import restore_amount_backup
            result = restore_amount_backup(args.package, paths['root'], cancel=cancelled.is_set, progress=progress)
        elif args.command == 'restore-many':
            phase = 'operation'
            from .restore import restore_amount_backups
            result = restore_amount_backups(args.package, paths['root'], cancel=cancelled.is_set, progress=progress)
        elif args.command == 'snapshot':
            phase = 'operation'
            from .backup import snapshot_amount_catalog
            result = snapshot_amount_catalog(paths['root'], paths['backup'])
        elif args.command == 'history':
            from .catalog import Catalog
            if not (paths['root'] / 'history.sqlite').exists(): raise ValueError('历史库不存在')
            catalog = Catalog(paths['root'])
            result = {'status': 'OK', 'total': catalog.count(), 'rows': catalog.list_page(args.offset, args.limit)}
        elif args.command == 'group':
            phase = 'operation'
            from .catalog import Catalog
            from .ui.group_index import ResultStore
            store = ResultStore(Catalog(paths['root']), args.batch_id, paths['cache'])
            result = {'status': 'OK', 'batch_id': args.batch_id, **store.get_group(args.group)}
        elif args.command in version_commands:
            phase = 'operation'
            from .versioning import SUPPORT_TABLE, inspect_amount_catalog, migrate_amount_catalog, prevent_destructive_index_rollback
            if args.command == 'version-info':
                result = {'status': 'SUPPORTED', 'support': SUPPORT_TABLE, 'index': inspect_amount_catalog(paths['root'])}
            elif args.command == 'migrate-index':
                result = migrate_amount_catalog(paths['root'], paths['backup'])
            else:
                result = prevent_destructive_index_rollback(paths['root'], args.snapshot_file)
        code = 5 if result.get('status') == 'CANCELLED' else (3 if result.get('status') in ('PARTIAL', 'FAILED') else 0)
    except UnsafeOutputPath as error:
        phase = 'preflight'; code = 2
        result = {'status': 'ERROR', 'error': str(error), 'exit_code': code}
    except (CancelledError, KeyboardInterrupt) as error:
        code = 5; result = {'status': 'CANCELLED', 'error': str(error), 'batch_id': current_bid}
    except ValueError as error:
        from .service import AuditFailed
        code = 5 if isinstance(error, AuditFailed) and error.outcome and error.outcome.status == 'CANCELLED' else (2 if phase in ('preflight', 'input') else 3)
        result = {'status': 'ERROR', 'error': str(error), 'batch_id': getattr(error, 'batch_id', None) or current_bid, 'exit_code': code}
    except Exception as error:
        code = 4; result = {'status': 'ERROR', 'error': f'{type(error).__name__}: {error}', 'batch_id': current_bid, 'exit_code': code}
    finally:
        signal.signal(signal.SIGINT, previous_signal)
        release_console_handler()
    # A rejected path must not create even an error receipt or log.
    if result_path is not None and phase != 'preflight':
        try:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            synced_write(receipt_temp, canonical_json(result))
            receipt_temp.replace(result_path)
        except Exception as error:
            result = {'status': 'RECEIPT_IO_ERROR', 'error': str(error), 'operation_result': result,
                      'batch_id': result.get('batch_id') if isinstance(result, dict) else current_bid, 'exit_code': 4}
            code = 4
    log_event(logger, {'event': 'command_finished', 'exit_code': code, 'result': result})
    if logger:
        for handler in logger.handlers[:]: handler.close(); logger.removeHandler(handler)
    _print(result, error=code != 0)
    return code
