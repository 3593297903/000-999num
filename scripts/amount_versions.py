"""First-release QA fixtures and actual candidate migration/rollback acceptance.

There is no real previous AmountTool release. The 1000 index is explicitly an
unpublished compatibility fixture; all original fixed bytes/anchors are copied.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import traceback
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from tests.amount_mode.support_release import (ReleaseHarness, assert_audit, canonical, fixed_hashes,
                                               read_json, sha, tree_fingerprint, write_json)


def backup_index(source, target):
    with closing(sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(target)) as copied:
            original.backup(copied)


def prepare(qa, ledger):
    from number_app.amount_mode.catalog import Catalog
    from number_app.amount_mode.versioning import _known_schemas, _schema_signature
    record = next(json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()
                  if json.loads(line).get("label") == "startup-seed")
    original_receipt = read_json(record["receipt_path"])
    source_batch = Path(record["batch_dir"])
    source_data = source_batch.parent.parent
    assert original_receipt["status"] == "COMPLETED" and original_receipt["checks_passed"] == 15
    assert sha(record["receipt_path"]) == record["receipt_sha256"]
    before = fixed_hashes(source_batch)
    root = qa / "versions" / ("v21-" + uuid.uuid4().hex[:6])
    fixture = root / "fixture"
    fixture.mkdir(parents=True)
    backup_index(source_data / "history.sqlite", fixture / "history.sqlite")
    with closing(sqlite3.connect(fixture / "history.sqlite")) as connection:
        assert connection.execute("SELECT count(*) FROM batches").fetchone()[0] == 1
    shutil.copytree(source_batch, fixture / "batches" / record["batch_id"])
    preupgrade_package = root / "preupgrade-package"
    shutil.copytree(original_receipt["backup"]["package_path"], preupgrade_package)
    known = _known_schemas()
    with closing(sqlite3.connect(fixture / "history.sqlite")) as connection:
        Catalog._configure(connection, set())
        assert _schema_signature(connection) in (known[0], known[2])
        connection.execute("ALTER TABLE batches DROP COLUMN backup_status")
        connection.execute("ALTER TABLE batches DROP COLUMN backup_error")
        connection.execute("PRAGMA user_version=1000")
        connection.commit()
        assert _schema_signature(connection) == known[1]
    future = root / "future1002"
    shutil.copytree(fixture, future)
    with closing(sqlite3.connect(future / "history.sqlite")) as connection:
        connection.execute("PRAGMA user_version=1002"); connection.commit()
    assert fixed_hashes(source_batch) == before
    assert fixed_hashes(fixture / "batches" / record["batch_id"]) == before
    manifest = {"status": "PREPARED_NOT_EXECUTABLE_ACCEPTANCE", "fixture_provenance": "unpublished_qa_fixture_not_a_previous_amount_tool_release",
                "real_previous_amount_tool_release_exists": False, "source_ledger": str(ledger), "seed_record": record,
                "source_receipt_sha256": sha(record["receipt_path"]), "fixture_root": str(fixture), "future_root": str(future),
                "preupgrade_package": str(preupgrade_package), "preupgrade_package_tree": tree_fingerprint(preupgrade_package),
                "expected_fixed_hashes": before, "fixture_index_sha256": sha(fixture / "history.sqlite"),
                "legacy_schema_signature_sha256": hashlib.sha256(canonical(known[1])).hexdigest(),
                "versioning_source_sha256": sha(PROJECT / "src/number_app/amount_mode/versioning.py"),
                "script_source_sha256": sha(__file__), "root": str(root)}
    write_json(root / "PREPARED.json", manifest)
    print(json.dumps({"status": manifest["status"], "manifest": str(root / "PREPARED.json"), "batch_id": record["batch_id"]}), flush=True)
    return 0


def run(qa, selected, manifest_file):
    prepared = read_json(manifest_file)
    assert prepared["status"] == "PREPARED_NOT_EXECUTABLE_ACCEPTANCE"
    assert prepared["versioning_source_sha256"] == sha(PROJECT / "src/number_app/amount_mode/versioning.py")
    fixture, future, root = (Path(prepared[key]) for key in ("fixture_root", "future_root", "root"))
    assert sha(fixture / "history.sqlite") == prepared["fixture_index_sha256"], "Fixture already changed; do not repeat migration blindly"
    record = prepared["seed_record"]
    bid_a = record["batch_id"]
    amounts_a = tuple(map(int, record["amounts"]))
    harness = ReleaseHarness(selected, qa)
    write_json(harness.root / "version-start.json", {"prepared_manifest": str(manifest_file), "prepared_manifest_sha256": sha(manifest_file),
                                                   "script_source_sha256": sha(__file__), "exe_sha256": harness.exe_sha256})
    stages = []
    try:
        info, _, _ = harness.invoke("version-info", "--data-dir", fixture, "--backup-dir", root / "bk")
        assert info["status"] == "SUPPORTED" and info["index"]["database_version"] == 1000
        assert info["support"]["real_previous_amount_tool_release_exists"] is False
        stages.append("actual_1000_fixture_recognized")
        future_before = tree_fingerprint(future)
        rejected, _, _ = harness.invoke("version-info", "--data-dir", future, "--backup-dir", root / "future-bk", expected_exit=3)
        assert rejected["status"] == "ERROR" and tree_fingerprint(future) == future_before
        assert not (root / "future-bk").exists()
        stages.append("actual_future1002_refused_tree_unchanged")
        migrated, _, _ = harness.invoke("migrate-index", "--data-dir", fixture, "--backup-dir", root / "bk")
        assert migrated["status"] == "MIGRATED_QA_FIXTURE" and migrated["from_version"] == 1000 and migrated["to_version"] == 1001
        assert sha(migrated["snapshot_path"]) == migrated["snapshot_sha256"]
        assert fixed_hashes(fixture / "batches" / bid_a) == prepared["expected_fixed_hashes"]
        stages.append("actual_migration_snapshot_then_transaction")
        verified, _, _ = harness.invoke("verify", "--batch-dir", fixture / "batches" / bid_a,
                                         "--data-dir", fixture, "--backup-dir", root / "bk", executable=harness.verify)
        assert verified["status"] == "PASS" and verified["provenance"] == "local_anchored"
        assert_audit(fixture / "batches" / bid_a, verified, amounts_a, harness.dist, harness.verify)
        assert fixed_hashes(fixture / "batches" / bid_a) == prepared["expected_fixed_hashes"]
        stages.append("migrated_original_fixed_bytes_fresh15")
        package_a, _, _ = harness.invoke("backup", "--batch-id", bid_a, "--destination", root / "bk",
                                          "--data-dir", fixture, "--backup-dir", root / "bk")
        assert package_a["status"] == "COMPLETE" and package_a["latest_audit_covered"] is True
        saved = harness.data, harness.backups
        harness.data, harness.backups = fixture, root / "bk"
        try:
            post = harness.generate("post-upgrade-B", (3, 7))
        finally:
            harness.data, harness.backups = saved
        result_b = post["result"]
        bid_b = result_b["batch_id"]
        before_rollback_index = sha(fixture / "history.sqlite")
        before_rollback_batches = tree_fingerprint(fixture / "batches")
        package_b = result_b["backup"]
        packages_before = {"A": tree_fingerprint(package_a["package_path"]), "B": tree_fingerprint(package_b["package_path"])}
        denied, _, _ = harness.invoke("rollback-check", "--snapshot-file", migrated["snapshot_path"],
                                       "--data-dir", fixture, "--backup-dir", root / "bk", expected_exit=3)
        assert denied["status"] == "ERROR" and bid_b in denied["error"]
        assert sha(fixture / "history.sqlite") == before_rollback_index
        assert tree_fingerprint(fixture / "batches") == before_rollback_batches
        assert tree_fingerprint(package_a["package_path"]) == packages_before["A"]
        assert tree_fingerprint(package_b["package_path"]) == packages_before["B"]
        stages.append("rollback_loss_of_B_refused_original_new_batches_packages_unchanged")
        switched = root / "program-switch" / "AmountTool"
        shutil.copytree(harness.dist, switched)
        assert {name: sha(switched / name) for name in harness.exe_sha256} == harness.exe_sha256
        switched_tool = switched / "AmountTool.exe"
        current, _, _ = harness.invoke("version-info", "--data-dir", fixture, "--backup-dir", root / "bk", executable=switched_tool)
        assert current["index"]["database_version"] == 1001
        history, _, _ = harness.invoke("history", "--data-dir", fixture, "--backup-dir", root / "bk", executable=switched_tool)
        assert {item["batch_id"] for item in history["rows"]} == {bid_a, bid_b}
        stages.append("same_binary_candidate_directory_switch_kept_A_and_B")
        offline = root / "fixture-source-unavailable"
        fixture.rename(offline)
        restored_root = root / "restored-many"
        try:
            assert not fixture.exists()
            restored, _, _ = harness.invoke("restore-many", "--package", package_a["package_path"], package_b["package_path"],
                                              "--data-dir", restored_root, "--backup-dir", root / "restore-bk", executable=switched_tool)
            assert restored["status"] == "COMPLETED" and restored["completed"] == 2 and restored["failed"] == 0
            assert not fixture.exists()
            expectations = {bid_a: amounts_a, bid_b: (3, 7)}
            for batch in restored["batches"]:
                assert batch["status"] == "COMPLETED" and batch["checks_passed"] == 15
                assert Path(batch["verified_target_root"]).resolve() == restored_root.resolve()
                assert_audit(batch["batch_dir"], batch, expectations[batch["batch_id"]], switched, switched_tool)
            stages.append("both_original_packages_independent_restore_many_each_fresh15_source_absent")
            broken = root / "bad-package-B"
            shutil.copytree(package_b["package_path"], broken)
            (broken / "batch" / "groups.txt").unlink()
            partial, _, _ = harness.invoke("restore-many", "--package", package_a["package_path"], broken,
                                             "--data-dir", root / "restore-mixed", "--backup-dir", root / "mixed-bk",
                                             expected_exit=3, executable=switched_tool)
            assert partial["status"] == "PARTIAL" and partial["completed"] == 1 and partial["failed"] == 1
            completed = [item for item in partial["batches"] if item["status"] == "COMPLETED"]
            assert len(completed) == 1 and completed[0]["batch_id"] == bid_a and completed[0]["checks_passed"] == 15
            assert_audit(completed[0]["batch_dir"], completed[0], amounts_a, switched, switched_tool)
            with closing(sqlite3.connect(root / "restore-mixed" / "history.sqlite")) as connection:
                assert connection.execute("SELECT batch_id FROM batches WHERE generation_status='COMPLETED'").fetchall() == [(bid_a,)]
            stages.append("mixed_valid_bad_package_partial_exit3_only_good_batch_valid")
        finally:
            if not fixture.exists(): offline.rename(fixture)
        assert tree_fingerprint(package_a["package_path"]) == packages_before["A"]
        assert tree_fingerprint(package_b["package_path"]) == packages_before["B"]
        assert tree_fingerprint(Path(prepared["preupgrade_package"])) == prepared["preupgrade_package_tree"]
        summary = {"status": "PASS_FOR_EXECUTED_CASES", "stages": stages, "exe_sha256": harness.exe_sha256,
                   "manifest": str(manifest_file), "root": str(harness.root), "fixture_root": str(fixture),
                   "batch_A": bid_a, "post_upgrade_batch_B": bid_b, "migration_snapshot": migrated,
                   "package_A": package_a, "package_B": package_b, "restore_many_result": restored,
                   "switched_program_dir": str(switched), "real_previous_amount_tool_release_exists": False,
                   "not_claimed": ["real_previous_release_upgrade", "actual_exe_mid_transaction_failure_injection", "bulk_restore_cancel_signal"]}
        write_json(harness.root / "version-acceptance.json", summary)
        print(json.dumps({"status": summary["status"], "stages": stages, "report": str(harness.root / "version-acceptance.json")}), flush=True)
        return 0
    except BaseException as error:
        write_json(harness.root / "version-acceptance.json", {"status": "FAILED", "stages_completed": stages,
                                                             "exe_sha256": harness.exe_sha256,
                                                             "error": type(error).__name__ + ": " + str(error),
                                                             "traceback": traceback.format_exc(), "manifest": str(manifest_file)})
        print(type(error).__name__ + ": " + str(error), file=sys.stderr, flush=True)
        return 3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-root", required=True, type=Path)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--seed-ledger", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--dist-dir", type=Path)
    args = parser.parse_args()
    qa = args.qa_root.resolve(strict=True)
    assert qa.is_relative_to((PROJECT / "qa/amount_mode").resolve())
    if args.prepare:
        assert args.seed_ledger
        return prepare(qa, args.seed_ledger)
    assert args.manifest and args.dist_dir
    return run(qa, args.dist_dir, args.manifest)


if __name__ == "__main__":
    raise SystemExit(main())
