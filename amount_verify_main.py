import multiprocessing
import sys

if __name__ == '__main__':
    if '--multiprocessing-fork' in sys.argv:
        from number_app.amount_mode import audit
        from number_app.amount_mode.checks import a_count, b_sort, c_ids
    multiprocessing.freeze_support()
    if not sys.argv[1:]:
        from number_app.amount_mode.ui.app import run_gui
        raise SystemExit(run_gui(start_in_verify=True))
    from number_app.amount_mode.cli import main
    from number_app.amount_mode.windows_console import attach_cli_console
    attach_cli_console()
    args = sys.argv[1:]
    raise SystemExit(main(args if args[0] == 'verify' else ['verify', *args]))
