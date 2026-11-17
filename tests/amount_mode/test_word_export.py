"""Word export checks use hand-written text, not generated number batches."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from number_app.amount_mode import word_export as module


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
SOURCE = (
    "第1组\n000.007直选1元\n128直选5元\n\n"
    "第2组\n999直选50元\n\n"
    "第3组\n010.111直选2元\n222直选3元\n"
)
EXPECTED_PARAGRAPHS = [
    "第一组", "000.007直选1元", "128直选5元", "",
    "第二组", "999直选50元", "",
    "第三组", "010.111直选2元", "222直选3元",
]


def read_paragraphs(path):
    """Independent OOXML reader; do not call the product's formatter/parser."""
    with ZipFile(path) as archive:
        assert archive.testzip() is None
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= set(archive.namelist())
        document = ET.fromstring(archive.read("word/document.xml"))
    return [
        "".join(text.text or "" for text in paragraph.iter("{" + W + "}t"))
        for paragraph in document.findall("w:body/w:p", NS)
    ]


def rewrite_document(path, mutation):
    with ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    document = ET.fromstring(members["word/document.xml"])
    mutation(document)
    members["word/document.xml"] = ET.tostring(document, encoding="utf-8", xml_declaration=True)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def test_docx_contains_all_exact_body_lines_and_one_blank_between_groups(tmp_path):
    path = tmp_path / "complete.docx"
    written = module._write_docx(path, SOURCE, 3)
    assert read_paragraphs(path) == EXPECTED_PARAGRAPHS
    assert written == {"group_count": 3, "paragraph_count": len(EXPECTED_PARAGRAPHS)}
    checked = module._verify_docx(path, SOURCE, 3)
    assert checked == written
    assert read_paragraphs(path)[-1] == "222直选3元"


def test_many_groups_keep_tail_and_chinese_headings_across_ten_thousand(tmp_path):
    # This writes a small compressed document, with no pool generation or ABC jobs.
    group_count = 10001
    source = "\n\n".join(f"第{n}组\n000.999直选{n}元" for n in range(1, group_count + 1)) + "\n"
    path = tmp_path / "all-groups.docx"
    result = module._write_docx(path, source, group_count)
    paragraphs = read_paragraphs(path)
    assert len(paragraphs) == result["paragraph_count"] == group_count * 3 - 1
    expected_labels = {1: "第一组", 10: "第十组", 11: "第十一组", 20: "第二十组", 100: "第一百组", 101: "第一百零一组", 1000: "第一千组", 10000: "第一万组", 10001: "第一万零一组"}
    for number, title in expected_labels.items():
        assert paragraphs[(number - 1) * 3] == title
    assert paragraphs[-2:] == ["第一万零一组", "000.999直选10001元"]
    assert sum(text == "" for text in paragraphs) == group_count - 1
    assert module._verify_docx(path, source, group_count) == result


@pytest.mark.parametrize("corruption", ["amount", "last_group", "duplicate_group"])
def test_readback_rejects_wrong_money_missing_tail_and_duplicate_group(tmp_path, corruption):
    path = tmp_path / "changed.docx"
    module._write_docx(path, SOURCE, 3)

    def mutate(document):
        if corruption == "last_group":
            body = document.find("w:body", NS)
            paragraphs = body.findall("w:p", NS)
            first_tail = next(i for i, p in enumerate(paragraphs) if "".join(t.text or "" for t in p.iter("{" + W + "}t")) == "第三组")
            for paragraph in paragraphs[first_tail:]:
                body.remove(paragraph)
            return
        old, new = ("000.007直选1元", "000.007直选9元") if corruption == "amount" else ("第二组", "第一组")
        node = next(t for t in document.iter("{" + W + "}t") if t.text == old)
        node.text = new

    rewrite_document(path, mutate)
    with pytest.raises(ValueError):
        module._verify_docx(path, SOURCE, 3)


def test_readback_rejects_declared_group_count_mismatch(tmp_path):
    path = tmp_path / "count.docx"
    module._write_docx(path, SOURCE, 3)
    with pytest.raises(ValueError):
        module._verify_docx(path, SOURCE, 4)


def test_low_level_readback_cancellation_does_not_change_document(tmp_path):
    path = tmp_path / "cancel-read.docx"
    module._write_docx(path, SOURCE, 3)
    before = path.read_bytes()
    with pytest.raises(module.WordExportCancelled):
        module._verify_docx(path, SOURCE, 3, cancel=lambda: True)
    assert path.read_bytes() == before


@pytest.fixture
def saved_batch(tmp_path, monkeypatch):
    root = tmp_path / "data"
    source = root / "batches" / "fixed-batch"
    source.mkdir(parents=True)
    # Admission only checks existence here; Catalog itself is deliberately mocked.
    (root / "history.sqlite").write_bytes(b"")
    (source / "groups.txt").write_bytes(SOURCE.encode("utf-8"))
    from number_app.amount_mode.protocol import AuditContext, MODE, CHECKER_VERSION
    context = AuditContext(MODE, 'fixed-batch', 'initial-read', CHECKER_VERSION,
                           (1,), 'b' * 64, 'a' * 64, 3)
    calls = []
    transitions = []

    def protect(*paths, **kwargs):
        calls.append("protect")
        assert kwargs["workspace_roots"] == (paths[0],)
        assert tuple(map(Path, kwargs["file_paths"])) == tuple(map(Path, paths[1:]))
        return tuple(map(Path, paths))

    @contextmanager
    def lock(path):
        calls.append("lock")
        assert Path(path) == root
        yield

    class Catalog:
        def __init__(self, path):
            calls.append("catalog")
            assert Path(path) == root

        def path(self, batch_id):
            assert batch_id == "fixed-batch"
            return source

        def transition(self, batch_id, state, **kwargs):
            transitions.append((batch_id, state, kwargs))

    def require_usable(catalog, batch_id):
        calls.append("usable")
        assert batch_id == "fixed-batch"
        return context if calls.count('usable') == 1 else context.with_audit('new-read-' + str(calls.count('usable')))

    def snapshot(path, actual_context):
        calls.append("snapshot")
        assert Path(path) == source and actual_context is context
        return {"groups.txt": (source / "groups.txt").read_bytes()}

    def unchanged(path, actual_context):
        calls.append("unchanged")
        assert Path(path) == source and actual_context is context

    monkeypatch.setattr(module, "protect_paths", protect)
    monkeypatch.setattr(module, "WorkspaceLock", lock)
    monkeypatch.setattr(module, "Catalog", Catalog)
    monkeypatch.setattr(module, "require_usable", require_usable)
    monkeypatch.setattr(module, "read_snapshot", snapshot)
    monkeypatch.setattr(module, "assert_snapshot_unchanged", unchanged)
    return SimpleNamespace(root=root, source=source, calls=calls, transitions=transitions)


def test_export_publishes_verified_full_document_and_preserves_saved_text(tmp_path, saved_batch):
    destination = tmp_path / "exports" / "complete.docx"
    before = (saved_batch.source / "groups.txt").read_bytes()
    result = module.export_amount_word(saved_batch.root, "fixed-batch", destination)
    assert result["status"] == "WORD_EXPORTED" and result["readback_verified"] is True
    assert result["group_count"] == 3 and result["paragraph_count"] == 10
    assert Path(result["path"]) == destination
    assert result["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert read_paragraphs(destination) == EXPECTED_PARAGRAPHS
    assert saved_batch.calls[:4] == ["protect", "lock", "catalog", "usable"]
    assert saved_batch.calls.count("unchanged") == 2
    assert (saved_batch.source / "groups.txt").read_bytes() == before
    assert not list(destination.parent.glob("*.partial"))


def test_existing_destination_is_never_overwritten(tmp_path, saved_batch):
    destination = tmp_path / "existing.docx"
    destination.write_bytes(b"existing user document")
    with pytest.raises(FileExistsError):
        module.export_amount_word(saved_batch.root, "fixed-batch", destination)
    assert destination.read_bytes() == b"existing user document"
    assert saved_batch.calls == ["protect"]


def test_invalid_destination_is_rejected_before_storage_or_directory_creation(tmp_path, saved_batch, monkeypatch):
    destination = tmp_path / "must-not-exist" / "result.docx"

    def refuse(*args, **kwargs):
        raise ValueError("QA protected destination")

    monkeypatch.setattr(module, "protect_paths", refuse)
    with pytest.raises(ValueError, match="protected destination"):
        module.export_amount_word(saved_batch.root, "fixed-batch", destination)
    assert saved_batch.calls == []
    assert not destination.parent.exists()


def test_immediate_cancellation_does_not_open_catalog_or_publish(tmp_path, saved_batch):
    destination = tmp_path / "cancelled" / "result.docx"
    with pytest.raises(module.WordExportCancelled):
        module.export_amount_word(saved_batch.root, "fixed-batch", destination, cancel=lambda: True)
    assert saved_batch.calls == ["protect"]
    assert not destination.parent.exists()


def test_cancellation_after_writing_begins_cleans_temporary_and_never_publishes(tmp_path, saved_batch):
    destination = tmp_path / "cancelled-during-write" / "result.docx"
    state = {"cancel": False, "writing_started": False}

    def progress(event):
        if event["phase"] == "word_writing":
            state["writing_started"] = True
            state["cancel"] = True

    with pytest.raises(module.WordExportCancelled):
        module.export_amount_word(saved_batch.root, "fixed-batch", destination, cancel=lambda: state["cancel"], progress=progress)
    assert state["writing_started"] is True
    assert not destination.exists()
    assert not list(destination.parent.iterdir())
    assert saved_batch.transitions == []


def test_failed_readback_cannot_publish(tmp_path, saved_batch, monkeypatch):
    destination = tmp_path / "readback-failed" / "result.docx"

    def fail(*args, **kwargs):
        raise ValueError("QA wrong final paragraph")

    monkeypatch.setattr(module, "_verify_docx", fail)
    with pytest.raises(ValueError, match="wrong final paragraph"):
        module.export_amount_word(saved_batch.root, "fixed-batch", destination)
    assert not destination.exists()
    assert not list(destination.parent.iterdir())


def test_destination_created_during_readback_is_preserved(tmp_path, saved_batch, monkeypatch):
    destination = tmp_path / "racing.docx"
    original_verify = module._verify_docx

    def verify_then_create(*args, **kwargs):
        result = original_verify(*args, **kwargs)
        destination.write_bytes(b"concurrent user document")
        return result

    monkeypatch.setattr(module, "_verify_docx", verify_then_create)
    with pytest.raises(FileExistsError):
        module.export_amount_word(saved_batch.root, "fixed-batch", destination)
    assert destination.read_bytes() == b"concurrent user document"
    assert not list(destination.parent.glob("*.partial"))


@pytest.mark.parametrize("fail_at", [1, 2])
def test_source_change_before_or_after_publish_withdraws_word(tmp_path, saved_batch, monkeypatch, fail_at):
    destination = tmp_path / "source-changed" / "result.docx"
    state = {"checks": 0}

    def changed(*args):
        state["checks"] += 1
        if state["checks"] == fail_at:
            raise ValueError("QA source bytes changed")

    monkeypatch.setattr(module, "assert_snapshot_unchanged", changed)
    with pytest.raises(ValueError, match="source bytes changed"):
        module.export_amount_word(saved_batch.root, "fixed-batch", destination)
    assert not destination.exists()
    assert not list(destination.parent.iterdir())
    assert saved_batch.transitions[-1][:2] == ("fixed-batch", "DAMAGED")


@pytest.mark.parametrize("fail_at", [2, 3])
def test_audit_evidence_changed_before_or_after_publication_cannot_succeed(tmp_path, saved_batch, monkeypatch, fail_at):
    destination = tmp_path / "audit-changed" / "result.docx"
    usable = module.require_usable
    calls = 0

    def evidence_changed(*args):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise ValueError("QA original audit evidence changed")
        return usable(*args)

    monkeypatch.setattr(module, "require_usable", evidence_changed)
    with pytest.raises(ValueError, match="original audit evidence changed"):
        module.export_amount_word(saved_batch.root, "fixed-batch", destination)
    assert not destination.exists()
    assert not list(destination.parent.iterdir())
