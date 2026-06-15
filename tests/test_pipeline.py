from pathlib import Path

from lucid_vault_exporter.pipeline import run_api_phase
from lucid_vault_exporter.state import StateDB


class FakeClient:
    def __init__(self):
        self.docs = [
            {"documentId": "d1", "title": "Uno", "product": "lucidchart",
             "parent": None, "pageCount": 1},
            {"documentId": "d2", "title": "Dos", "product": "lucidspark",
             "parent": None, "pageCount": 1},
        ]

    def search_documents(self, **kw):
        yield from self.docs

    def get_folder(self, folder_id):
        return None

    def export_page_png(self, doc_id, *, page):
        from lucid_vault_exporter.lucid_client import PageNotFound
        if page > 1:
            raise PageNotFound("x")
        return b"\x89PNG" + doc_id.encode()


def test_api_phase_end_to_end(tmp_path: Path):
    vault = tmp_path / "vault"
    with StateDB.open(vault) as db:
        stats = run_api_phase(FakeClient(), db, vault)
    assert stats["documents"] == 2 and stats["png_ok"] == 2
    notes = list(vault.glob("*.md"))
    assert len(notes) == 2


def test_api_phase_resume_skips_done(tmp_path: Path):
    vault = tmp_path / "vault"
    client = FakeClient()
    with StateDB.open(vault) as db:
        run_api_phase(client, db, vault)
        stats2 = run_api_phase(client, db, vault)
    assert stats2["png_ok"] == 0  # nothing re-exported


def test_refresh_notes_adds_sidecar_links(tmp_path: Path):
    from lucid_vault_exporter.pipeline import refresh_notes
    vault = tmp_path / "vault"
    with StateDB.open(vault) as db:
        # first run the API phase so docs, pngs, and notes exist
        run_api_phase(FakeClient(), db, vault)
        # simulate the browser phase having produced a PDF sidecar for d1
        pdf_path = vault / "d1-sidecar.pdf"
        pdf_path.write_bytes(b"%PDF fake")
        db.set_artifact("d1", "pdf", "ok", path=str(pdf_path))
        # re-render notes
        count = refresh_notes(db, vault)
        assert count == 2  # both docs re-rendered
        # d1's note must now link the pdf sidecar by basename
        d1_note = db.get_document("d1")["note_path"]
        text = Path(d1_note).read_text(encoding="utf-8")
        assert "[[d1-sidecar.pdf]]" in text
        # d1's note must still embed its PNG page (glob by doc_id found it)
        assert "![[" in text and ".png]]" in text


def test_api_phase_cancel_raises(tmp_path: Path):
    import pytest

    from lucid_vault_exporter.control import Cancelled, Control

    vault = tmp_path / "vault"
    ctrl = Control()
    ctrl.cancel()  # pre-cancelled: the first checkpoint raises
    with StateDB.open(vault) as db, pytest.raises(Cancelled):
        run_api_phase(FakeClient(), db, vault, control=ctrl)
