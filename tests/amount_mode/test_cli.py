import json
from pathlib import Path
import subprocess
import sys

from number_app.amount_mode.cli import main


def invoke(*args):
    return subprocess.run([sys.executable, '-m', 'number_app.amount_mode', *map(str, args)],
                          capture_output=True, text=True, encoding='utf-8', timeout=150)


def test_source_cli_generate_verify_and_invalid_config(tmp_path):
    config = tmp_path / 'input.json'; config.write_text('{"set_count":3,"amounts":["1","5","2"]}', encoding='utf-8')
    root = tmp_path / 'data'; receipt = tmp_path / 'generate.json'
    process = invoke('generate', '--config', config, '--data-dir', root, '--result-json', receipt)
    assert process.returncode == 0, process.stderr
    result = json.loads(receipt.read_text(encoding='utf-8'))
    assert result['status'] == 'COMPLETED' and result['checks_passed'] == 15
    assert result['raw_total'] == 3000 and result['per_code_expected_amount'] == '8' and result['total_amount'] == '8000'
    receipt2 = tmp_path / 'verify.json'
    verified = invoke('verify', '--batch-dir', result['batch_dir'], '--data-dir', root, '--result-json', receipt2)
    assert verified.returncode == 0, verified.stderr
    assert json.loads(receipt2.read_text(encoding='utf-8'))['audit_id'] != result['audit_id']
    config.write_text('{"set_count":1,"amounts":["0"]}', encoding='utf-8')
    bad = invoke('generate', '--config', config, '--data-dir', root, '--result-json', tmp_path / 'bad.json')
    assert bad.returncode == 2
    assert json.loads((tmp_path / 'bad.json').read_text(encoding='utf-8'))['status'] == 'ERROR'
    groups = Path(result['batch_dir']) / 'groups.txt'
    groups.write_bytes(groups.read_bytes() + b'\n')
    damaged = invoke('verify', '--batch-dir', result['batch_dir'], '--data-dir', root, '--result-json', tmp_path / 'damaged.json')
    assert damaged.returncode == 3 and json.loads((tmp_path / 'damaged.json').read_text(encoding='utf-8'))['status'] == 'ERROR'


def test_unsafe_receipt_refuses_before_any_write(tmp_path):
    config = tmp_path / 'input.json'; config.write_text('{"set_count":1,"amounts":["1"]}', encoding='utf-8')
    external = tmp_path / 'external'; external.mkdir()
    (external / 'request.json').write_bytes(b'broken-marker')
    (external / 'manifest.json').write_bytes(b'broken-marker')
    target = external / 'not_created' / 'result.json'
    root = tmp_path / 'not_created_data'
    process = invoke('generate', '--config', config, '--data-dir', root, '--result-json', target)
    assert process.returncode == 2
    assert not root.exists() and not target.parent.exists()


def test_receipt_io_error_preserves_completed_batch_and_cancel_exit(tmp_path, monkeypatch):
    from number_app.amount_mode import cli, service
    from number_app.amount_mode.catalog import Catalog
    from number_app.amount_mode.artifacts import CancelledError
    config = tmp_path / 'input.json'; config.write_text('{"set_count":1,"amounts":["1"]}', encoding='utf-8')
    root = tmp_path / 'data'
    def failed_receipt(*args, **kwargs): raise OSError('QA receipt disk failure')
    monkeypatch.setattr(cli, 'synced_write', failed_receipt)
    assert main(['generate', '--config', str(config), '--data-dir', str(root), '--result-json', str(tmp_path / 'receipt.json')]) == 4
    assert Catalog(root).count('COMPLETED') == 1
    monkeypatch.undo()
    def cancel(*args, **kwargs): raise CancelledError('QA cancelled')
    monkeypatch.setattr(service, 'generate_amount_batch', cancel)
    assert main(['generate', '--config', str(config), '--data-dir', str(root), '--result-json', str(tmp_path / 'cancel.json')]) == 5
    assert json.loads((tmp_path / 'cancel.json').read_text(encoding='utf-8'))['status'] == 'CANCELLED'
