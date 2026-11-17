"""Real completed fixtures, fixed collision groups and poisoned offset evidence."""
from dataclasses import replace
from pathlib import Path
import shutil
import uuid
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from number_app.amount_mode.artifacts import write_amount_candidate
from number_app.amount_mode.audit import audit_amount_batch
from number_app.amount_mode.catalog import Catalog
from number_app.amount_mode.input_config import build_request_bytes
from number_app.amount_mode.protocol import AuditContext, CHECKER_VERSION, FIXED_FILES, MODE, canonical_json, digest, strict_json
from number_app.amount_mode.spec import AmountBatchSpec
from number_app.amount_mode.ui.group_index import ResultStore, cache_info, clear_memory_cache
from number_app.amount_mode.ui.results import ResultsWidget
from number_app.amount_mode.ui.statistics import StatisticsWidget
from .conftest import RUN


def _complete(catalog, amounts, leading=()):
    bid = uuid.uuid4().hex
    spec = AmountBatchSpec(amounts)
    request = build_request_bytes(spec, batch_id=bid)
    catalog.create(spec, request)
    catalog.transition(bid, "GENERATING")
    sizes = [len(group) for group in leading]
    identities = [identity for group in leading for identity in group]
    used = set(identities)
    remaining = [identity for identity in range(spec.raw_total) if identity not in used]
    assert len(remaining) % 2 == 0
    identities += remaining
    sizes += [2] * (len(remaining) // 2)
    catalog.transition(bid, "WRITING")
    result = write_amount_candidate(catalog.path(bid), spec, request, sizes, identities)
    context = AuditContext(MODE, bid, uuid.uuid4().hex, CHECKER_VERSION, amounts,
                           digest(request), result["snapshot_digest"], len(sizes))
    catalog.freeze(bid, context)
    catalog.transition(bid, "AUDITING")
    outcome = audit_amount_batch(catalog.path(bid), context)
    assert outcome.passed, outcome.errors
    catalog.record_audit(bid, outcome)
    catalog.transition(bid, "COMMITTING")
    final = catalog.root / "batches" / bid
    final.parent.mkdir(exist_ok=True)
    catalog.path(bid).rename(final)
    catalog.transition(bid, "COMMITTING", batch_relpath="batches/" + bid)
    catalog.complete(bid, replace(outcome, session_dir=final / "audits" / outcome.context.audit_id))
    return bid


@pytest.fixture(scope="module")
def completed_fixture():
    root = RUN / "results-fixture" / uuid.uuid4().hex[:8]
    catalog = Catalog(root)
    bids = [_complete(catalog, (1, 5), ((7, 1007), (128, 1128), (192, 1192)))]
    bids.extend(_complete(catalog, (value,)) for value in (2, 3, 4))
    return root, bids


@pytest.fixture
def saved(tmp_path, completed_fixture):
    root, bids = completed_fixture
    target = tmp_path / "data"
    shutil.copytree(root, target)
    clear_memory_cache()
    return Catalog(target), bids


def _fixed_hashes(catalog, bid):
    return {name: digest((catalog.path(bid) / name).read_bytes()) for name in FIXED_FILES}


def test_single_code_actual_sources_all_code_statistics_and_clipboard(qtbot, saved):
    catalog, bids = saved
    bid = bids[0]
    before = _fixed_hashes(catalog, bid)
    store = ResultStore(catalog, bid)
    group = store.get_group(1)
    assert group["body"] == "007直选6元\n"
    assert group["raw_count"] == 2 and group["distinct_count"] == 1 and group["total_amount"] == "6"
    assert [row["item_id"] for row in group["raw_records"]] == [7, 1007]
    assert store.get_group(store.group_count)["raw_count"] == 2
    stats = store.get_statistics()
    assert len(stats) == 1000 and stats[0]["code"] == "000" and stats[-1]["code"] == "999"
    assert all(row["actual_raw"] == 2 and row["actual_amount"] == "6" and row["raw_difference"] == 0 and row["amount_difference"] == "0" for row in stats)
    results = ResultsWidget()
    qtbot.addWidget(results)
    results.open_batch(catalog, bid, store)
    results.show()
    qtbot.mouseClick(results.copy_button, Qt.MouseButton.LeftButton)
    assert QApplication.clipboard().text() == "007直选6元\n"
    assert results.source_model.rowCount() == 2
    results.group_spin.setValue(2)
    qtbot.mouseClick(results.copy_button, Qt.MouseButton.LeftButton)
    assert QApplication.clipboard().text() == "128直选6元\n"
    statistics = StatisticsWidget()
    qtbot.addWidget(statistics)
    statistics.set_store(store)
    assert statistics.table_model.rowCount() == 1000
    assert statistics.table_model.data(statistics.table_model.index(999, 0)) == "999"
    assert _fixed_hashes(catalog, bid) == before


def test_matching_hash_wrong_offset_is_rebuilt_for_selected_group(saved):
    catalog, bids = saved
    store = ResultStore(catalog, bids[0])
    value = strict_json(store.cache_file.read_bytes())
    old_groups_hash = value["groups_sha256"]
    for key in ("text_offsets", "item_offsets"):
        value[key][0:2] = value[key][1:3]
    store.cache_file.write_bytes(canonical_json(value))
    assert strict_json(store.cache_file.read_bytes())["groups_sha256"] == old_groups_hash
    clear_memory_cache()
    reopened = ResultStore(catalog, bids[0])
    assert reopened.get_group(1)["body"] == "007直选6元\n"
    repaired = strict_json(reopened.cache_file.read_bytes())
    assert repaired["text_offsets"][0] == repaired["item_offsets"][0] == 0
    assert repaired["groups_sha256"] == old_groups_hash


def test_lru_and_disk_derived_cache_limits(saved):
    catalog, bids = saved
    stores = [ResultStore(catalog, bid) for bid in bids]
    info = cache_info()
    assert info["batches"] == 3 and info["bytes"] <= 128 * 1024**2
    assert len(list(stores[-1].cache_root.glob("amount-index-*.json"))) <= 3
    assert stores[0].get_group(1)["body"] == "007直选6元\n"
    assert cache_info()["batches"] <= 3
    assert len(list(stores[-1].cache_root.glob("amount-index-*.json"))) <= 3


def test_damage_or_latest_cancel_stops_view_and_copy(qtbot, saved):
    catalog, bids = saved
    bid = bids[0]
    store = ResultStore(catalog, bid)
    results = ResultsWidget()
    qtbot.addWidget(results)
    results.set_store(store)
    original = catalog.get(bid)
    path = catalog.path(bid) / "groups.txt"
    path.write_bytes(path.read_bytes().replace(b"007", b"008", 1))
    assert results.copy_current_body() is None
    assert not results.copy_button.isEnabled() and not results.body_edit.toPlainText()
    with pytest.raises(ValueError): ResultStore(catalog, bid)
    assert catalog.get(bid)["generation_status"] == "DAMAGED"
    assert catalog.get(bid)["manifest_sha256"] == original["manifest_sha256"]
    other = bids[1]
    catalog.mark_verification(other, "CANCELLED")
    with pytest.raises(ValueError): ResultStore(catalog, other)
    report_bid = bids[2]
    report_store = ResultStore(catalog, report_bid)
    report_row = catalog.get(report_bid)
    report = catalog.path(report_bid) / "audits" / report_row["latest_audit_id"] / "A_1.json"
    report.write_bytes(report.read_bytes() + b"\n")
    with pytest.raises(ValueError): report_store.get_group(1)
    with pytest.raises(ValueError): ResultStore(catalog, report_bid)
    assert catalog.get(report_bid)["latest_verification_status"] == "ERROR"


def test_cache_target_protected_before_creation(saved, monkeypatch):
    catalog, bids = saved
    bid = bids[0]
    target = catalog.path(bid) / "new-cache"
    hashes = _fixed_hashes(catalog, bid)
    row = catalog.get(bid)
    with pytest.raises(ValueError): ResultStore(catalog, bid, cache_root=target)
    assert not target.exists() and _fixed_hashes(catalog, bid) == hashes and catalog.get(bid) == row
    store = ResultStore(catalog, bid)
    import number_app.amount_mode.ui.group_index as index_module
    def fail_publish(*args, **kwargs):
        raise OSError("QA cache publication failure")
    monkeypatch.setattr(index_module.os, "replace", fail_publish)
    store._write_offsets(store._entry())
    assert store.cache_warning and not list(store.cache_root.glob("*.tmp"))
    assert _fixed_hashes(catalog, bid) == hashes
