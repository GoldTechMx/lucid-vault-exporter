from pathlib import Path

from lucid_vault_exporter.exporter_api import export_document_pngs
from lucid_vault_exporter.lucid_client import LucidError, PageNotFound
from lucid_vault_exporter.state import StateDB


class FakeClient:
    def __init__(self, pages_by_doc):
        self.pages = pages_by_doc  # doc_id -> number of pages

    def export_page_png(self, doc_id, *, page):
        if page > self.pages.get(doc_id, 0):
            raise PageNotFound(f"{doc_id} p{page}")
        return b"\x89PNG" + bytes(f"{doc_id}-{page}", "ascii")


def setup_doc(tmp_path, page_count=None):
    db = StateDB(tmp_path / "s.sqlite")
    db.upsert_document(document_id="d1", title="Mapa", product="lucidchart",
                       folder_path="A/B", page_count=page_count)
    return db


def test_exports_known_page_count(tmp_path: Path):
    db = setup_doc(tmp_path, page_count=2)
    doc = db.get_document("d1")
    written = export_document_pngs(FakeClient({"d1": 2}), db, doc, tmp_path / "vault")
    assert len(written) == 2
    assert (tmp_path / "vault" / "A" / "B" / "_assets" / "Mapa d1 p1.png").is_file()
    assert db.get_artifact("d1", "png")["status"] == "ok"


def test_discovers_pages_when_count_unknown(tmp_path: Path):
    db = setup_doc(tmp_path, page_count=None)
    doc = db.get_document("d1")
    written = export_document_pngs(FakeClient({"d1": 3}), db, doc, tmp_path / "vault")
    assert len(written) == 3


def test_failure_marks_artifact_failed(tmp_path: Path):
    class Boom:
        def export_page_png(self, doc_id, *, page):
            raise LucidError("403 forbidden")
    db = setup_doc(tmp_path, page_count=1)
    doc = db.get_document("d1")
    written = export_document_pngs(Boom(), db, doc, tmp_path / "vault")
    assert written == []
    assert db.get_artifact("d1", "png")["status"] == "failed"
