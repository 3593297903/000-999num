import multiprocessing
import sys

if __name__ == '__main__':
    if '--multiprocessing-fork' in sys.argv:
        # Resolve worker targets from this bundle before spawn preparation can
        # replace sys.path. No Qt or application window is initialized here.
        from number_app.amount_mode import audit
        from number_app.amount_mode.checks import a_count, b_sort, c_ids
    multiprocessing.freeze_support()
    if sys.argv[1:] and sys.argv[1] != 'gui':
        from number_app.amount_mode.windows_console import attach_cli_console
        attach_cli_console()
    from number_app.amount_mode.cli import main
    raise SystemExit(main(sys.argv[1:] or ['gui']))
