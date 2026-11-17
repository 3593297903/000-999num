"""QA-only deterministic fault boundary; never packaged or production-imported."""
import json
from pathlib import Path
import sys
import time
from number_app.amount_mode.service import generate_amount_batch


def main():
    root, boundary, marker = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    def progress(event):
        phase = event.get('state', event.get('phase'))
        if phase == boundary:
            marker.write_text(json.dumps(event, ensure_ascii=False), encoding='utf-8')
            time.sleep(60)
    generate_amount_batch((1, 5, 2), root, progress=progress)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
