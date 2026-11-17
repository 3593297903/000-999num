import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import pytest

from number_app.amount_mode.catalog import Catalog
from number_app.amount_mode.service import generate_amount_batch, AuditFailed
from number_app.amount_mode.recovery import verify_amount_batch, recover_amount_batch
from number_app.amount_mode.protocol import FIXED_FILES


def hashes(root):
    return {name: hashlib.sha256((Path(root) / name).read_bytes()).hexdigest() for name in FIXED_FILES}


def test_generate_one_three_and_independent_reverify(tmp_path):
    for amounts in ((1,), (1, 5, 2)):
        root = tmp_path / str(len(amounts))
        events = []
        result = generate_amount_batch(amounts, root, progress=events.append)
        assert result['status'] == 'COMPLETED' and result['checks_passed'] == 15
        assert result['raw_total'] == 1000 * len(amounts)
        assert result['per_code_expected_amount'] == str(sum(amounts))
        original = hashes(result['batch_dir'])
        verified = verify_amount_batch(result['batch_dir'], root)
        assert verified['status'] == 'PASS' and verified['audit_id'] != result['audit_id']
        assert hashes(result['batch_dir']) == original
        assert len([e for e in events if e.get('phase') == 'check_finished' and e['status'] == 'PASS']) == 15
        db = sqlite3.connect(root / 'history.sqlite')
        try:
            assert db.execute('SELECT COUNT(*) FROM audit_checks').fetchone()[0] == 30
        finally:
            db.close()


def test_cancel_before_and_after_committing(tmp_path):
    for late in (False, True):
        root = tmp_path / str(late)
        flag = [False]
        seen = []
        def progress(event):
            seen.append(event)
            if event.get('state') == ('COMMITTING' if late else 'BEFORE_COMMITTING'):
                flag[0] = True
        if late:
            result = generate_amount_batch((1,), root, cancel=lambda: flag[0], progress=progress)
            assert result['status'] == 'COMPLETED'
        else:
            from number_app.amount_mode.artifacts import CancelledError
            with pytest.raises(CancelledError): generate_amount_batch((1,), root, cancel=lambda: flag[0], progress=progress)
            catalog = Catalog(root)
            row = catalog.list_page()[0]
            assert row['generation_status'] == 'CANCELLED'
            assert not (root / 'batches' / row['batch_id']).exists()


def test_actual_hard_exits_and_new_fifteen_on_recovery(tmp_path):
    probe = Path(__file__).with_name('lifecycle_probe.py')
    for boundary in ('WRITING', 'FROZEN', 'check_finished', 'PUBLISHED'):
        root = tmp_path / boundary
        marker = tmp_path / (boundary + '.json')
        process = subprocess.Popen([sys.executable, str(probe), str(root), boundary, str(marker)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 90
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if not marker.exists():
            process.kill()
            out = process.communicate(timeout=10)[1]
            pytest.fail('fault marker not reached: ' + out.decode('utf-8', errors='replace'))
        event = json.loads(marker.read_text(encoding='utf-8'))
        process.kill()
        process.communicate(timeout=10)
        catalog = Catalog(root)
        bid = event['batch_id']
        row = catalog.get(bid)
        assert row['generation_status'] != 'COMPLETED'
        if boundary == 'WRITING':
            with pytest.raises(AuditFailed): recover_amount_batch(root, bid)
            assert Catalog(root).get(bid)['generation_status'] != 'COMPLETED'
        else:
            original_dir = root / ('batches' if boundary == 'PUBLISHED' else 'staging') / bid
            original = hashes(original_dir)
            result = recover_amount_batch(root, bid)
            assert result['status'] == 'COMPLETED' and result['checks_passed'] == 15
            assert hashes(result['batch_dir']) == original
            new_summary = json.loads((Path(result['batch_dir']) / 'audits' / result['audit_id'] / 'summary.json').read_text(encoding='utf-8'))
            assert len(new_summary['checks']) == 15
            if 'audit_id' in event: assert event['audit_id'] != result['audit_id']


def test_completed_damage_blocks_and_never_reanchors(tmp_path):
    root = tmp_path / 'data'
    result = generate_amount_batch((1,), root)
    catalog = Catalog(root)
    old = catalog.get(result['batch_id'])
    target = Path(result['batch_dir']) / 'groups.txt'
    target.write_bytes(target.read_bytes() + b'\n')
    with pytest.raises(ValueError): verify_amount_batch(result['batch_dir'], root)
    current = Catalog(root).get(result['batch_id'])
    assert current['generation_status'] == 'DAMAGED'
    assert current['request_sha256'] == old['request_sha256'] and current['manifest_sha256'] == old['manifest_sha256']
