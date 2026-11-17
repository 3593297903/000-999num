"""Publish a read-back checked copy on the destination volume, without rendering."""
from pathlib import Path
import shutil
import uuid

from number_app.storage.locking import WorkspaceLock
from .access import require_usable
from .catalog import Catalog
from .output_paths import protect_paths
from .protocol import FIXED_FILES, assert_snapshot_unchanged, digest


def export_amount_batch(data_root, batch_id, destination_root):
    # Every user-controlled root/final/temp path is checked before lock/Catalog/mkdir.
    destination = Path(destination_root)
    final = destination / batch_id
    temporary = destination / ('.' + batch_id + '.' + uuid.uuid4().hex + '.partial')
    root, destination, final, temporary = protect_paths(data_root, destination, final, temporary, workspace_roots=(data_root,))
    if final.exists():
        raise FileExistsError('导出目标已存在，拒绝覆盖')
    with WorkspaceLock(root):
        catalog = Catalog(root)
        context = require_usable(catalog, batch_id)
        source = catalog.path(batch_id)
        def source_still_trusted():
            try:
                assert_snapshot_unchanged(source, context)
            except (OSError, ValueError) as error:
                catalog.transition(batch_id, 'DAMAGED', error=str(error))
                raise
        originals = {name: digest((source / name).read_bytes()) for name in FIXED_FILES}
        # Only registered, committed sessions are part of the published evidence.
        reports = []
        for record in catalog.list_audits(batch_id):
            if not record['summary_sha256']:
                continue
            index = catalog.audit_record(record['audit_id'])
            reports.append((record['summary_relpath'], record['summary_sha256']))
            for entry in index['checks']:
                if entry['report_sha256']:
                    reports.append((entry['report_relpath'], entry['report_sha256']))
        for relative, sha in reports:
            file = source / relative
            if not file.is_file() or file.is_symlink() or file.is_junction() or digest(file.read_bytes()) != sha:
                raise ValueError('原审查报告缺失或变化：' + relative)
        destination.mkdir(parents=True, exist_ok=True)
        temporary.mkdir()
        for name in FIXED_FILES:
            shutil.copyfile(source / name, temporary / name)
        for relative, sha in reports:
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, target)
            if digest(target.read_bytes()) != sha:
                raise ValueError('导出报告复制后核对失败')
        for name, sha in originals.items():
            if digest((temporary / name).read_bytes()) != sha:
                raise ValueError('导出固定数据复制后核对失败：' + name)
        source_still_trusted()
        assert_snapshot_unchanged(temporary, context)
        if final.exists():
            raise FileExistsError('导出发布目标冲突，保留未完成临时副本')
        for relative, sha in reports:
            if digest((source / relative).read_bytes()) != sha:
                catalog.mark_verification(batch_id, 'ERROR', error='导出期间原审查报告变化')
                raise ValueError('导出期间原审查报告变化')
        temporary.rename(final)
        try:
            assert_snapshot_unchanged(final, context)
            source_still_trusted()
        except BaseException:
            # Preserve failed evidence under its explicitly unfinished name.
            final.rename(temporary)
            raise
        return {'status': 'EXPORTED', 'mode': context.mode, 'batch_id': batch_id,
                'path': str(final), 'export_dir': str(final), 'snapshot_digest': context.expected_manifest_sha256,
                'request_sha256': context.expected_request_sha256, 'provenance': context.provenance,
                'covered_audit_ids': sorted({Path(relative).parts[1] for relative, _ in reports})}
