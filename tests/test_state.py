from pathlib import Path

import pytest

from lucid_vault_exporter.state import StateDB


def open_db(tmp_path: Path) -> StateDB:
    return StateDB(tmp_path / "state.sqlite")


def test_upsert_and_get_document(tmp_path):
    with open_db(tmp_path) as db:
        db.upsert_document(document_id="d1", title="T", product="lucidchart",
                           folder_path="A/B", page_count=2)
        doc = db.get_document("d1")
        assert doc["title"] == "T" and doc["page_count"] == 2


def test_artifact_lifecycle(tmp_path):
    with open_db(tmp_path) as db:
        db.upsert_document(document_id="d1", title="T", product="lucidchart")
        db.set_artifact("d1", "png", "pending")
        db.set_artifact("d1", "png", "ok", path="A/B/T.png")
        rows = db.artifacts_by_status("png", ("ok",))
        assert rows[0]["path"] == "A/B/T.png"


def test_failed_artifact_records_error_and_count(tmp_path):
    with open_db(tmp_path) as db:
        db.upsert_document(document_id="d1", title="T", product="lucidchart")
        db.set_artifact("d1", "pdf", "failed", error="timeout")
        db.set_artifact("d1", "pdf", "failed", error="timeout again")
        row = db.get_artifact("d1", "pdf")
        assert row["retry_count"] == 2 and "again" in row["error"]


def test_invalid_kind_rejected(tmp_path):
    with open_db(tmp_path) as db:
        db.upsert_document(document_id="d1", title="T", product="lucidchart")
        with pytest.raises(ValueError):
            db.set_artifact("d1", "gif", "ok")


def test_pending_work_excludes_done(tmp_path):
    with open_db(tmp_path) as db:
        db.upsert_document(document_id="d1", title="T", product="lucidchart")
        db.upsert_document(document_id="d2", title="U", product="lucidspark")
        db.set_artifact("d1", "png", "ok")
        pending = db.documents_missing_artifact("png")
        assert [d["document_id"] for d in pending] == ["d2"]


def test_upsert_document_rejects_unknown_column(tmp_path):
    with open_db(tmp_path) as db, pytest.raises(ValueError):
        db.upsert_document(document_id="d1", bogus_col="x")


def test_documents_missing_includes_failed(tmp_path):
    with open_db(tmp_path) as db:
        db.upsert_document(document_id="d1", title="T", product="lucidchart")
        db.set_artifact("d1", "png", "failed", error="boom")
        missing = db.documents_missing_artifact("png")
        assert [d["document_id"] for d in missing] == ["d1"]


def test_reset_artifacts_sets_pending(tmp_path):
    with open_db(tmp_path) as db:
        db.upsert_document(document_id="d1", title="T", product="lucidchart")
        db.set_artifact("d1", "png", "ok", path="x.png")
        db.reset_artifacts("png")
        assert db.get_artifact("d1", "png")["status"] == "pending"
        # reset preserves path (COALESCE not involved here; just status changes)


def test_folder_roundtrip(tmp_path):
    with open_db(tmp_path) as db:
        db.upsert_folder("f1", "Clientes", None, "Clientes")
        db.upsert_folder("f2", "ACME", "f1", "Clientes/ACME")
        assert db.get_folder("f2")["path"] == "Clientes/ACME"
        assert db.get_folder("f2")["parent_id"] == "f1"
        assert db.get_folder("nope") is None


def test_record_and_read_errors(tmp_path):
    with open_db(tmp_path) as db:
        db.record_error("d1", "png_export", "403 forbidden")
        db.record_error("d2", "pdf_export", "timeout")
        errors = db.all_errors()
        assert len(errors) == 2
        assert {e["item_id"] for e in errors} == {"d1", "d2"}
