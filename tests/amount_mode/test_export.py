from pathlib import Path
import hashlib
import pytest
from number_app.amount_mode.service import generate_amount_batch
from number_app.amount_mode.recovery import verify_amount_batch
from number_app.amount_mode.export import export_amount_batch
from number_app.amount_mode.protocol import FIXED_FILES
from number_app.amount_mode.catalog import Catalog


def test_original_bytes_export_and_declaration_only_reverify(tmp_path):
    root = tmp_path / 'data'
    batch = generate_amount_batch((1,), root)
    result = export_amount_batch(root, batch['batch_id'], tmp_path / 'exports')
    assert result['status'] == 'EXPORTED'
    for name in FIXED_FILES:
        assert (Path(result['path']) / name).read_bytes() == (Path(batch['batch_dir']) / name).read_bytes()
    with pytest.raises(ValueError): verify_amount_batch(result['path'], root)
    checked = verify_amount_batch(result['path'], tmp_path / 'independent')
    assert checked['status'] == 'PASS' and checked['provenance'] == 'imported_declaration_only'
    assert Catalog(tmp_path / 'independent').count() == 0
    assert not (Path(result['path']) / 'input_anchor.json').exists()


def test_copy_failure_never_publishes_and_does_not_regenerate(tmp_path, monkeypatch):
    from number_app.amount_mode import export as module
    root = tmp_path / 'data'; batch = generate_amount_batch((1,), root)
    copy = module.shutil.copyfile
    def fail(source, target):
        if Path(source).name == 'items.jsonl': raise OSError('QA disk full')
        return copy(source, target)
    monkeypatch.setattr(module.shutil, 'copyfile', fail)
    with pytest.raises(OSError): export_amount_batch(root, batch['batch_id'], tmp_path / 'failed')
    assert not (tmp_path / 'failed' / batch['batch_id']).exists()
    assert Catalog(root).count('COMPLETED') == 1
    assert list((tmp_path / 'failed').glob('*.partial'))


def test_target_or_source_changes_are_detected_before_publication(tmp_path, monkeypatch):
    from number_app.amount_mode import export as module
    for corrupt_source in (False, True):
        root = tmp_path / str(corrupt_source); batch = generate_amount_batch((1,), root)
        copy = module.shutil.copyfile
        def change(source, target):
            value = copy(source, target)
            if Path(source).name == 'groups.txt':
                file = Path(source) if corrupt_source else Path(target)
                file.write_bytes(file.read_bytes() + b'\n')
            return value
        with monkeypatch.context() as patch:
            patch.setattr(module.shutil, 'copyfile', change)
            with pytest.raises(ValueError): export_amount_batch(root, batch['batch_id'], tmp_path / ('changed' + str(corrupt_source)))
        assert not (tmp_path / ('changed' + str(corrupt_source)) / batch['batch_id']).exists()
        if corrupt_source: assert Catalog(root).get(batch['batch_id'])['generation_status'] == 'DAMAGED'
