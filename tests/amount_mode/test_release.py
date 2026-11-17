"""Run only against the explicitly selected new AmountTool frozen candidate."""
from pathlib import Path
from contextlib import closing
import os
import shutil
import sqlite3
import subprocess
import uuid
import pytest

from .support_release import (ReleaseHarness, assert_audit, canonical, fixed_hashes, read_json,
                              sha, tree_fingerprint, write_json)


@pytest.fixture(scope="module")
def packaged(request):
    selected = request.config.getoption("--dist-dir")
    assert selected, "Release acceptance requires --dist-dir; no source or old NumberTool fallback"
    qa = Path(os.environ.get("AMOUNT_QA_ROOT", "E:/number/qa/amount_mode/r17-ddb2bc"))
    harness = ReleaseHarness(selected, qa)
    yield harness
    after = harness.current_hashes()
    write_json(harness.root / "candidate-after.json", {"exe_sha256": after, "unchanged": after == harness.exe_sha256})
    assert after == harness.exe_sha256


@pytest.fixture(scope="module")
def scales(packaged):
    h = packaged
    examples = h.fixture["complete_input_examples"]
    assert h.fixture["fixture_version"] == 2 and h.fixture["handoff_version"] == "1.1"
    inputs = [("S1", tuple(map(int, examples[0]["amounts"]))), ("S2", (2, 10)),
              ("S3", tuple(map(int, examples[1]["amounts"]))), ("S10", tuple(range(1, 11))),
              ("S100", tuple(i % 9 + 1 for i in range(100))),
              ("S300", tuple(map(int, examples[2]["amounts"]))),
              ("S2-big", tuple(map(int, examples[3]["amounts"])))]
    for label, amounts in inputs:
        h.generate(label, amounts)
    return h.batches


def test_packaged_scales_and_independent_largest_reverify(packaged, scales):
    h = packaged
    largest = scales["S300"]
    result = largest["result"]
    assert result["raw_total"] == 300000 and result["per_code_expected_amount"] == "1197" and result["total_amount"] == "1197000"
    before = fixed_hashes(result["batch_dir"])
    verified, command, directory = h.invoke("verify", "--batch-dir", result["batch_dir"], "--data-dir", h.data,
                                            "--backup-dir", h.backups, executable=h.verify)
    assert verified["status"] == "PASS" and verified["checks_passed"] == 15
    assert verified["audit_id"] != result["audit_id"] and verified["provenance"] == "local_anchored"
    audit = assert_audit(result["batch_dir"], verified, largest["amounts"], h.dist, h.verify)
    assert fixed_hashes(result["batch_dir"]) == before
    write_json(directory / "audit-inspected.json", audit)
    assert scales["S2-big"]["result"]["total_amount"] == "200000000000000022000"


def test_packaged_fixed_input_rejections_and_io_exit(packaged):
    h = packaged
    rejected = []
    for index, case in enumerate(h.fixture["invalid_examples"]):
        config = h.root / "bad-in" / f"config-{index:02d}.json"
        write_json(config, {key: case[key] for key in ("set_count", "amounts")})
        target = h.root / "bad-data" / f"c{index}"
        result, command, directory = h.invoke("generate", "--config", config, "--data-dir", target,
                                                "--backup-dir", h.root / "bad-bk", expected_exit=2)
        assert result["status"] == "ERROR"
        assert not (target / "batches").exists() and not (target / "staging").exists()
        rejected.append({"case": case["name"], "exit_code": command["exit_code"], "command_evidence": str(directory / "command.json")})
    for index, case in enumerate(h.fixture["invalid_json_inputs"]):
        config = h.root / "bad-in" / f"json-{index:02d}.json"
        config.write_text(case["raw_json"], encoding="utf-8")
        target = h.root / "bad-data" / f"j{index}"
        result, command, directory = h.invoke("generate", "--config", config, "--data-dir", target,
                                                "--backup-dir", h.root / "bad-bk", expected_exit=2)
        assert result["status"] == "ERROR" and not (target / "batches").exists()
        rejected.append({"case": case["name"], "exit_code": command["exit_code"], "command_evidence": str(directory / "command.json")})
    result, command, directory = h.invoke("generate", "--config", h.root / "missing-config.json",
                                            "--data-dir", h.root / "missing-input-data", "--backup-dir", h.root / "bad-bk", expected_exit=4)
    assert result["status"] == "ERROR" and command["exit_code"] == 4
    write_json(h.root / "fixed-input-rejections.json", {"status": "PASS", "cases": rejected, "io_exit_evidence": str(directory / "command.json")})


def test_packaged_export_backup_independent_restore_and_slots(packaged, scales):
    h = packaged
    sample = scales["S3"]
    result, amounts = sample["result"], sample["amounts"]
    original = fixed_hashes(result["batch_dir"])
    verified, _, directory = h.invoke("verify", "--batch-dir", result["batch_dir"], "--data-dir", h.data,
                                       "--backup-dir", h.backups, executable=h.verify)
    assert_audit(result["batch_dir"], verified, amounts, h.dist, h.verify)
    assert verified["audit_id"] != result["audit_id"]
    exported, _, _ = h.invoke("export", "--batch-id", result["batch_id"], "--destination", h.root / "out",
                               "--data-dir", h.data, "--backup-dir", h.backups)
    assert exported["status"] == "EXPORTED" and fixed_hashes(exported["export_dir"]) == original
    external, _, _ = h.invoke("verify", "--batch-dir", exported["export_dir"], "--data-dir", h.root / "external-index",
                               "--backup-dir", h.root / "external-bk", executable=h.verify)
    assert external["status"] == "PASS" and external["provenance"] == "imported_declaration_only"
    assert_audit(exported["export_dir"], external, amounts, h.dist, h.verify)
    with closing(sqlite3.connect(h.root / "external-index" / "history.sqlite")) as connection:
        assert connection.execute("SELECT count(*) FROM batches").fetchone()[0] == 0
    package, _, _ = h.invoke("backup", "--batch-id", result["batch_id"], "--destination", h.root / "manual-bk",
                              "--data-dir", h.data, "--backup-dir", h.backups)
    assert package["status"] == "COMPLETE" and package["latest_audit_covered"] is True
    package_path = Path(package["package_path"])
    assert (package_path / "input_anchor.json").is_file() and (package_path / "catalog_record.json").is_file()
    backup_tree = tree_fingerprint(package_path)
    reused, _, _ = h.invoke("backup", "--batch-id", result["batch_id"], "--destination", h.root / "manual-bk",
                             "--data-dir", h.data, "--backup-dir", h.backups)
    assert reused["status"] == "REUSED" and reused["package_path"] == str(package_path)
    assert tree_fingerprint(package_path) == backup_tree
    offline_source = h.root / "source-unavailable"
    h.data.rename(offline_source)
    restored_root = h.root / "restored"
    try:
        assert not h.data.exists()
        restored, _, restore_command = h.invoke("restore", "--package", package_path, "--data-dir", restored_root,
                                                 "--backup-dir", h.root / "restore-bk")
        assert not h.data.exists(), "Restore attempted to recreate/use the original data root"
        assert restored["status"] == "COMPLETED" and restored["checks_passed"] == 15
        assert Path(restored["verified_target_root"]).resolve() == restored_root.resolve()
        assert restored["audit_id"] not in (result["audit_id"], verified["audit_id"])
        assert fixed_hashes(restored["batch_dir"]) == original
        assert_audit(restored["batch_dir"], restored, amounts, h.dist, h.tool)
        write_json(restore_command / "source-unavailable-proof.json", {"original_root": str(h.data), "original_root_absent_during_restore": True,
                                                                     "held_original_root": str(offline_source), "target_root": str(restored_root)})
    finally:
        if not h.data.exists():
            offline_source.rename(h.data)
    assert tree_fingerprint(package_path) == backup_tree
    bad_package = h.root / "bad-package"
    shutil.copytree(package_path, bad_package)
    (bad_package / "batch" / "groups.txt").unlink()
    bad_target = h.root / "bad-restore"
    failed, _, _ = h.invoke("restore", "--package", bad_package, "--data-dir", bad_target,
                             "--backup-dir", h.root / "bad-restore-bk", expected_exit=3)
    assert failed["status"] == "ERROR" and not bad_target.exists()
    business_before = tree_fingerprint(h.data / "batches")
    for _ in range(3):
        snapshot, _, _ = h.invoke("snapshot", "--data-dir", h.data, "--backup-dir", h.backups)
        assert snapshot["status"] == "SNAPSHOT_COMPLETE" and snapshot["kind"] == "history_index_only_not_batch_backup"
    slots = h.backups / "catalog" / "slots"
    assert (slots / "A.json").is_file() and (slots / "B.json").is_file()
    assert len(list((h.backups / "catalog" / "snapshots").glob("*.sqlite"))) == 2
    for name in ("A", "B"):
        value = read_json(slots / (name + ".json"))
        path = h.backups / "catalog" / value["snapshot_relative"]
        assert path.is_file() and sha(path) == value["sha256"]
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert connection.execute("SELECT count(*) FROM batches").fetchone()[0] == len(scales)
    assert tree_fingerprint(h.data / "batches") == business_before and fixed_hashes(result["batch_dir"]) == original


def test_packaged_path_rejections_leave_all_source_bytes_and_status_unchanged(packaged, scales):
    h = packaged
    source = scales["S1"]["result"]
    other = scales["S2"]["result"]
    source_path, other_path = Path(source["batch_dir"]), Path(other["batch_dir"])
    legacy_external = h.root / "legacy-external"
    legacy_external.mkdir()
    (legacy_external / "request.json").write_bytes(b"QA legacy marker")
    (legacy_external / "quotas.json").write_bytes(b"QA legacy marker")
    junction = h.root / "linked-batch"
    link = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(other_path)], capture_output=True)
    write_json(h.root / "junction-command.json", {"command": ["cmd", "/c", "mklink", "/J", str(junction), str(other_path)],
                                                  "exit_code": link.returncode, "is_junction": junction.is_junction(),
                                                  "stdout": link.stdout.decode(errors="replace"), "stderr": link.stderr.decode(errors="replace")})
    assert link.returncode == 0 and junction.is_junction(), "A real Windows junction is required; no skipped link claim"
    destinations = [("backup", source_path), ("backup", other_path), ("export", other_path),
                    ("export", source_path / "not-existing" / "nested"),
                    ("backup", legacy_external / "not-existing"), ("export", junction / "not-existing"),
                    ("export", Path(str(other_path).upper()) / "not-existing"),
                    ("backup", h.data / "staging" / "not-existing")]
    for index, (operation, destination) in enumerate(destinations):
        before = tree_fingerprint(h.data)
        external_before = tree_fingerprint(legacy_external)
        receipt = h.root / "rejected-receipts" / f"{index}.json"
        result, command, directory = h.invoke(operation, "--batch-id", source["batch_id"], "--destination", destination,
                                               "--data-dir", h.data, "--backup-dir", h.backups,
                                               expected_exit=2, receipt_path=receipt, require_receipt=False)
        assert result is None and not receipt.exists()
        assert tree_fingerprint(h.data) == before and tree_fingerprint(legacy_external) == external_before
        write_json(directory / "unchanged-proof.json", {"status": "PASS", "destination": str(destination),
                                                       "source_tree_sha256": __import__("hashlib").sha256(canonical(before)).hexdigest(),
                                                       "source_entries": len(before), "no_receipt_written": True})
    before = tree_fingerprint(h.data)
    protected_receipt = other_path / "no-new-directory" / "receipt.json"
    _, _, _ = h.invoke("generate", "--config", scales["S1"]["config"], "--data-dir", h.data,
                         "--backup-dir", h.backups, expected_exit=2, receipt_path=protected_receipt, require_receipt=False)
    assert not protected_receipt.parent.exists() and tree_fingerprint(h.data) == before


def test_packaged_fingerprint_damage_and_future_protocol_are_not_accepted(packaged, scales):
    h = packaged
    result = scales["S1"]["result"]
    native = h.root / "damaged-native"
    native.mkdir()
    shutil.copy2(h.data / "history.sqlite", native / "history.sqlite")
    copied = native / "batches" / result["batch_id"]
    shutil.copytree(result["batch_dir"], copied)
    (copied / "groups.txt").write_bytes((copied / "groups.txt").read_bytes() + b"\n")
    rejected, _, _ = h.invoke("verify", "--batch-dir", copied, "--data-dir", native,
                               "--backup-dir", h.root / "damaged-bk", executable=h.verify, expected_exit=3)
    assert rejected["status"] == "ERROR"
    with closing(sqlite3.connect(native / "history.sqlite")) as connection:
        row = connection.execute("SELECT generation_status,request_sha256,manifest_sha256 FROM batches WHERE batch_id=?", (result["batch_id"],)).fetchone()
    assert row == ("DAMAGED", result["request_sha256"], result["snapshot_digest"])
    future = h.root / "future-protocol"
    shutil.copytree(result["batch_dir"], future)
    request = read_json(future / "request.json")
    request["schema_version"] = 999
    (future / "request.json").write_bytes(canonical(request))
    failed, _, _ = h.invoke("verify", "--batch-dir", future, "--data-dir", h.root / "future-index",
                             "--backup-dir", h.root / "future-bk", executable=h.verify, expected_exit=3)
    assert failed["status"] == "ERROR"
    with closing(sqlite3.connect(h.root / "future-index" / "history.sqlite")) as connection:
        assert connection.execute("SELECT count(*) FROM batches WHERE generation_status='COMPLETED'").fetchone()[0] == 0
