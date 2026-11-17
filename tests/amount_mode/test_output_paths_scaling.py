"""Real Windows metadata costs and reparse protection for native index scaling."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

import pytest
from number_app.amount_mode import output_paths


def _registry(root, rows):
    root.mkdir()
    connection = sqlite3.connect(root / 'history.sqlite')
    try:
        connection.execute('CREATE TABLE batches(batch_id TEXT,batch_relpath TEXT,generation_status TEXT)')
        connection.executemany('INSERT INTO batches VALUES(?,?,?)', rows)
        connection.commit()
    finally:
        connection.close()


def _junction(alias, target):
    process = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(alias), str(target)], capture_output=True, timeout=10)
    assert process.returncode == 0, (process.stdout, process.stderr)
    assert alias.is_junction() and alias.resolve() == target.resolve()


def _check_link_cases(base, linker):
    """Also callable by a privileged QA probe for real symbolic links."""
    root = base / 'data'
    _registry(root, [('registered','batches/registered','FAILED'),('deep','batches/deep','INTERRUPTED')])
    (root / 'batches').mkdir(); (root / 'staging').mkdir()
    targets = [base / name for name in ('registered-target','orphan-target','deep-target')]
    for target in targets:
        target.mkdir(); (target / 'sentinel.txt').write_bytes(b'original')
    linker(root / 'batches/registered', targets[0])
    linker(root / 'staging/orphan-unregistered', targets[1])
    (root / 'batches/deep').mkdir()
    linker(root / 'batches/deep/nested-link', targets[2])
    protected = [targets[0]/'missing/deeper', targets[1]/'missing/deeper',
                 root/'batches/deep/nested-link/missing',
                 Path(str(root/'batches/registered').swapcase())/'missing']
    before = {str(target):(target/'sentinel.txt').read_bytes() for target in targets}
    failures = []
    for target in protected:
        with pytest.raises(ValueError) as error:
            output_paths.protect_paths(target, workspace_roots=(root,))
        failures.append({'target':str(target),'error':str(error.value)})
        assert not target.exists()
    assert before == {str(target):(target/'sentinel.txt').read_bytes() for target in targets}
    ordinary = base/'normal-output'
    assert output_paths.protect_paths(ordinary,workspace_roots=(root,))[0] == ordinary.resolve()
    return {'status':'PASS','registry_kind':'path_metadata_fixture_not_completed_batch_evidence','cases':failures}


def test_ten_thousand_native_leaves_keep_actual_preflight_cost_and_resolutions_bounded(tmp_path, monkeypatch):
    root = tmp_path/'data'
    _registry(root, [(f'qa_{i}',f'staging/qa_{i}','CREATED') for i in range(10000)])
    (root/'staging').mkdir();(root/'batches').mkdir()
    # Exercise ordinary entries as well as the many not-yet-created leaves.
    for index in range(100):
        (root/'staging'/f'qa_{index}').mkdir()
    monkeypatch.setattr(output_paths,'DEFAULT_ROOTS',(root,))
    before = (root/'history.sqlite').read_bytes()
    original = Path.resolve
    calls = []
    def counted(path,*args,**kwargs):
        calls.append(str(path))
        return original(path,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'resolve',counted)  # delegates every actual Windows call
        started = time.monotonic()
        safe, = output_paths.protect_paths(tmp_path/'safe',workspace_roots=(root,))
        elapsed = time.monotonic()-started
    assert safe == (tmp_path/'safe').resolve()
    assert len(calls)<100 and elapsed<2, (len(calls),elapsed)
    assert not safe.exists() and (root/'history.sqlite').read_bytes()==before
    with pytest.raises(ValueError):
        output_paths.protect_paths(root/'staging/not-created/deeper',workspace_roots=(root,))
    (tmp_path/'native-scaling.json').write_text(json.dumps({'created_rows':10000,'real_completed':0,
        'windows_elapsed_seconds':elapsed,'actual_resolve_calls':len(calls),'resolve_calls':calls,
        'index_sha256':hashlib.sha256(before).hexdigest(),'no_write':True},indent=2),encoding='utf-8')


def test_registered_and_orphan_native_junctions_and_deep_alias_keep_protection(tmp_path):
    report = _check_link_cases(tmp_path,_junction)
    (tmp_path/'native-junctions.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


def test_custom_native_registration_fallback_and_bad_file_remain_fail_closed(tmp_path, monkeypatch):
    root = tmp_path/'data'
    absolute = tmp_path/'absolute-outside';absolute.mkdir()
    relative = tmp_path/'relative-outside';relative.mkdir()
    _registry(root,[('custom_abs',str(absolute),'FAILED'),('custom_rel','../relative-outside','CANCELLED')])
    monkeypatch.setattr(output_paths,'DEFAULT_ROOTS',(root,))
    for target in (absolute/'absent',relative/'absent'):
        with pytest.raises(ValueError):
            output_paths.protect_paths(target,workspace_roots=(root,))
        assert not target.exists()
    (root/'batches').mkdir()
    (root/'batches/file-root').write_bytes(b'not a batch directory')
    connection=sqlite3.connect(root/'history.sqlite')
    try:
        connection.execute('INSERT INTO batches VALUES(?,?,?)',('file-root','batches/file-root','CREATED'))
        connection.commit()
    finally:connection.close()
    with pytest.raises(ValueError,match='不是目录'):
        output_paths.protect_paths(tmp_path/'otherwise-safe',workspace_roots=(root,))
    assert not (tmp_path/'otherwise-safe').exists()


def test_external_marked_ancestor_survives_child_link_resolution(tmp_path):
    def fingerprint(root):
        result={}
        def visit(path):
            relative=path.relative_to(root).as_posix()
            if path.is_symlink() or path.is_junction():
                result[relative]=['link',str(path.resolve())]
            elif path.is_dir():
                result[relative]=['dir']
                for child in path.iterdir():visit(child)
            else:
                result[relative]=['file',hashlib.sha256(path.read_bytes()).hexdigest()]
        visit(root)
        return result
    reports=[]
    for name,markers in (('batch',('request.json','manifest.json')),
                         ('backup-complete',('BACKUP_COMPLETE.json','backup_manifest.json')),
                         ('backup-anchor',('input_anchor.json','catalog_record.json'))):
        marked=tmp_path/name;marked.mkdir()
        for marker in markers:(marked/marker).write_bytes(b'QA deliberately damaged marker, still protected\n')
        outside=tmp_path/(name+'-ordinary');outside.mkdir()
        (outside/'keep.txt').write_bytes(b'original ordinary target')
        alias=marked/'child';_junction(alias,outside)
        target=alias/'not-created/deeper'
        before={'marked':fingerprint(marked),'outside':fingerprint(outside)}
        error=None;returned=None
        try:
            returned=output_paths.protect_paths(target,workspace_roots=(tmp_path/'workspace',))[0]
        except ValueError as caught:error=str(caught)
        after={'marked':fingerprint(marked),'outside':fingerprint(outside)}
        reports.append({'case':name,'error':error,'returned':str(returned),'before':before,'after':after,
                        'trees_unchanged':before==after,'refused':error is not None})
        assert before==after and not target.exists()
    ordinary=tmp_path/'normal-ordinary';ordinary.mkdir()
    normal_alias=tmp_path/'normal-alias';_junction(normal_alias,ordinary)
    normal=output_paths.protect_paths(normal_alias/'new',workspace_roots=(tmp_path/'workspace',))[0]
    assert normal==(ordinary/'new').resolve()
    (tmp_path/'external-marker-links.json').write_text(json.dumps({'cases':reports,
        'independent_normal_junction_allowed':str(normal)},ensure_ascii=False,indent=2),encoding='utf-8')
    assert all(report['refused'] for report in reports), reports
