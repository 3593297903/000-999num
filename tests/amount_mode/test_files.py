from dataclasses import replace
import pytest
from number_app.amount_mode.protocol import read_snapshot, strict_json, canonical_json, digest, FIXED_FILES
from .helpers import candidate


@pytest.mark.parametrize("amounts", [(1,), (1, 5, 2)])
def test_actual_five_files(tmp_path, amounts):
    root = tmp_path / "batch"
    ctx = candidate(root, amounts)
    buffers = read_snapshot(root, ctx)
    assert set(buffers) == set(FIXED_FILES)
    assert all(data.endswith(b"\n") and b"\r" not in data and not data.startswith(b"\xef\xbb\xbf") for data in buffers.values())
    rows = [strict_json(line) for line in buffers["items.jsonl"].splitlines()]
    assert len(rows) == 1000 * len(amounts)
    assert {r["item_id"] for r in rows} == set(range(len(rows)))
    assert all(r["amount"] == str(amounts[r["set_no"] - 1]) for r in rows)
    assert all(canonical_json(strict_json(buffers[n])) == buffers[n] for n in ("request.json", "group_plan.json", "manifest.json"))


def test_snapshot_fails_on_mutation_and_extra_file(tmp_path):
    root = tmp_path / "batch"
    ctx = candidate(root, (1,))
    p = root / "groups.txt"
    original = p.read_bytes()
    p.write_bytes(original + b"\n")
    with pytest.raises(ValueError): read_snapshot(root, ctx)
    p.write_bytes(original)
    (root / "extra").write_text("not fixed")
    with pytest.raises(ValueError): read_snapshot(root, ctx)


def test_no_current_file_reanchoring(tmp_path):
    root = tmp_path / "batch"
    ctx = candidate(root, (1,))
    with pytest.raises(ValueError): read_snapshot(root, replace(ctx, expected_request_sha256="0" * 64))
    with pytest.raises(ValueError): read_snapshot(root, replace(ctx, expected_manifest_sha256="0" * 64))
