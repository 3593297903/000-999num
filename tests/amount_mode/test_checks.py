import ast
import importlib
import json
from pathlib import Path
import pytest
from number_app.amount_mode.protocol import canonical_json, strict_json, COMMON_PROOFS, EXTRA_PROOFS
from .helpers import candidate, reseal

FIXTURE = json.loads((Path(__file__).parents[2] / '按套金额随机小组_验收样例.json').read_text(encoding='utf-8'))


def checkers():
    return [getattr(importlib.import_module('number_app.amount_mode.checks.' + module), 'check_' + method.lower())
            for module, method in [('a_count', 'A'), ('b_sort', 'B'), ('c_ids', 'C')]]


def rows(root):
    return [strict_json(line) for line in (root / 'items.jsonl').read_bytes().splitlines()]


def write_rows(root, records):
    (root / 'items.jsonl').write_bytes(b''.join(canonical_json(r) for r in records))


def qa_rebuild_text(root, records):
    groups = {}
    for row in records:
        money = groups.setdefault(row['group'], {})
        money[row['code']] = money.get(row['code'], 0) + int(row['amount'])
    bodies = []
    for group, money in groups.items():
        lines = []
        for value in sorted(set(money.values())):
            lines.append('.'.join(code for code in money if money[code] == value) + '直选' + str(value) + '元')
        bodies.append(f'第{group}组\n' + '\n'.join(lines))
    (root / 'groups.txt').write_bytes(('\n\n'.join(bodies) + '\n').encode('utf-8'))


def assert_rejected(root, ctx):
    for check in checkers():
        with pytest.raises(ValueError): check(root, ctx)


def test_all_manual_collisions_as_complete_files(tmp_path):
    for index, fixture in enumerate(FIXTURE['cases']):
        root = tmp_path / str(index)
        amounts = tuple(map(int, fixture['set_amounts']))
        ctx = candidate(root, amounts, [tuple(row['item_id'] for row in fixture['records'])])
        first = (root / 'groups.txt').read_text(encoding='utf-8').split('\n\n')[0]
        assert first == '第1组\n' + fixture['expected_body'].rstrip('\n')
        for method, check in zip('ABC', checkers()):
            result = check(root, ctx)
            assert result['actual_raw_total'] == len(amounts) * 1000
            assert result['actual_total_amount'] == str(sum(amounts) * 1000)
            assert result['checked_source_code_cells'] == len(amounts) * 1000
            assert all(result['proofs'][key] is True for key in (*COMMON_PROOFS, *EXTRA_PROOFS[method]))


def test_balanced_sources_preserve_all_margins_but_rejected(tmp_path):
    root = tmp_path / 'source'
    ctx = candidate(root, (1, 1))
    records = rows(root)
    replacements = {1007: 7, 8: 1008}
    for row in records:
        if row['item_id'] in replacements:
            row['item_id'] = replacements[row['item_id']]
            row['set_no'] = row['item_id'] // 1000 + 1
    assert [sum(r['set_no'] == source for r in records) for source in (1, 2)] == [1000, 1000]
    assert all(sum(r['code'] == f'{code:03d}' for r in records) == 2 for code in range(1000))
    write_rows(root, records)
    assert_rejected(root, reseal(root, ctx))


def test_balanced_group_swap_same_group_and_global_amounts_rejected(tmp_path):
    root = tmp_path / 'swap'
    ctx = candidate(root, (1, 5), [(7, 1128), (1007, 128)])
    pieces = (root / 'groups.txt').read_text(encoding='utf-8').split('\n\n')
    pieces[0] = '第1组\n128直选1元\n007直选5元'
    pieces[1] = '第2组\n007直选1元\n128直选5元'
    (root / 'groups.txt').write_bytes('\n\n'.join(pieces).encode('utf-8'))
    assert_rejected(root, reseal(root, ctx))


def test_all_000_replaced_without_count_or_total_change(tmp_path):
    root = tmp_path / 'missing000'
    ctx = candidate(root, (1, 1))
    records = rows(root)
    for row in records:
        if row['code'] == '000':
            row['item_id'] += 1
            row['code'] = '001'
    assert len(records) == 2000 and sum(int(r['amount']) for r in records) == 2000
    write_rows(root, records)
    qa_rebuild_text(root, records)
    assert_rejected(root, reseal(root, ctx))


def test_reanchored_input_self_consistent_still_rejected(tmp_path):
    root = tmp_path / 'request'
    ctx = candidate(root, (1, 5))
    request = strict_json((root / 'request.json').read_bytes())
    request['sets'][1]['amount'] = '9'
    (root / 'request.json').write_bytes(canonical_json(request))
    records = rows(root)
    for row in records:
        if row['set_no'] == 2: row['amount'] = '9'
    write_rows(root, records)
    qa_rebuild_text(root, records)
    assert_rejected(root, reseal(root, ctx))


def test_malformed_records_text_versions_and_hashes(tmp_path):
    mutations = ('missing', 'extra', 'money', 'position', 'code', 'id_bool', 'leadingzero', 'money_zero',
                 'fullwidth_dot', 'text_tail', 'double_amount_line', 'no_header', 'version', 'hash')
    for mutation in mutations:
        root = tmp_path / mutation
        ctx = candidate(root, (1, 5), [(7, 1128), (1007, 128)])
        records = rows(root)
        if mutation == 'missing': records.pop()
        elif mutation == 'extra': records.append(records[-1])
        elif mutation in ('money', 'position', 'code', 'id_bool'):
            field, value = {'money': ('amount', '5'), 'position': ('position', 2), 'code': ('code', '008'), 'id_bool': ('item_id', True)}[mutation]
            records[0][field] = value
        if mutation in ('missing', 'extra', 'money', 'position', 'code', 'id_bool'): write_rows(root, records)
        elif mutation == 'version':
            plan = strict_json((root / 'group_plan.json').read_bytes())
            plan['schema_version'] = 999
            (root / 'group_plan.json').write_bytes(canonical_json(plan))
        else:
            original = (root / 'groups.txt').read_text(encoding='utf-8')
            replacements = {'leadingzero': ('007直选1元', '7直选1元'), 'money_zero': ('直选1元', '直选0元'),
                            'fullwidth_dot': ('007直选1元', '007．008直选1元'), 'text_tail': ('元\n', '元x\n'),
                            'double_amount_line': ('007直选1元', '007直选1元\n009直选1元'),
                            'no_header': ('第1组\n', ''), 'hash': ('直选1元', '直选2元')}
            before, after = replacements[mutation]
            (root / 'groups.txt').write_bytes(original.replace(before, after, 1).encode('utf-8'))
        if mutation != 'hash': ctx = reseal(root, ctx)
        assert_rejected(root, ctx)


def test_checkers_business_implementations_are_separate():
    for check in checkers():
        path = Path(importlib.import_module(check.__module__).__file__)
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in ('formatting', 'artifacts', 'spec', 'input_config', 'a_count', 'b_sort', 'c_ids')
