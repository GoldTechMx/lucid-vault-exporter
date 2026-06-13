from pathlib import Path

from lucid_vault_exporter.exporter_browser import BrowserExporter
from lucid_vault_exporter.state import StateDB


class FakeDriver:
    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids)
        self.calls = []

    def download_export(self, doc_id: str, fmt: str, *, product: str) -> bytes:
        self.calls.append((doc_id, fmt))
        if doc_id in self.fail_ids:
            raise RuntimeError("export dialog never appeared")
        return f"{fmt}-bytes-{doc_id}".encode()


def setup(tmp_path, docs):
    db = StateDB(tmp_path / "s.sqlite")
    for d in docs:
        db.upsert_document(**d)
    return db


def test_downloads_pdf_and_vsdx_for_lucidchart(tmp_path: Path):
    db = setup(tmp_path, [{"document_id": "d1", "title": "Mapa", "product": "lucidchart",
                           "folder_path": "A"}])
    drv = FakeDriver()
    exp = BrowserExporter(drv, db, tmp_path / "vault", formats=["pdf", "vsdx"], delay=lambda: None)
    exp.run()
    assert (tmp_path / "vault" / "A" / "Mapa d1.pdf").is_file()
    assert (tmp_path / "vault" / "A" / "Mapa d1.vsdx").is_file()
    assert db.get_artifact("d1", "pdf")["status"] == "ok"


def test_vsdx_skipped_for_lucidspark(tmp_path: Path):
    db = setup(tmp_path, [{"document_id": "d2", "title": "Board", "product": "lucidspark",
                           "folder_path": ""}])
    drv = FakeDriver()
    exp = BrowserExporter(drv, db, tmp_path / "vault", formats=["pdf", "vsdx"], delay=lambda: None)
    exp.run()
    assert ("d2", "vsdx") not in drv.calls
    assert db.get_artifact("d2", "vsdx")["status"] == "skipped"


def test_failure_recorded_and_run_continues(tmp_path: Path):
    db = setup(tmp_path, [
        {"document_id": "bad", "title": "X", "product": "lucidchart", "folder_path": ""},
        {"document_id": "ok1", "title": "Y", "product": "lucidchart", "folder_path": ""},
    ])
    exp = BrowserExporter(FakeDriver(fail_ids={"bad"}), db, tmp_path / "vault",
                          formats=["pdf"], delay=lambda: None)
    exp.run()
    assert db.get_artifact("bad", "pdf")["status"] == "failed"
    assert db.get_artifact("ok1", "pdf")["status"] == "ok"


def test_resume_skips_completed(tmp_path: Path):
    db = setup(tmp_path, [{"document_id": "d1", "title": "Mapa", "product": "lucidchart",
                           "folder_path": ""}])
    db.set_artifact("d1", "pdf", "ok")
    drv = FakeDriver()
    BrowserExporter(drv, db, tmp_path / "vault", formats=["pdf"], delay=lambda: None).run()
    assert drv.calls == []


def test_run_returns_stats_and_passes_product(tmp_path: Path):
    db = setup(tmp_path, [
        {"document_id": "d1", "title": "Mapa", "product": "lucidchart", "folder_path": ""},
        {"document_id": "d2", "title": "Board", "product": "lucidspark", "folder_path": ""},
    ])

    seen_products = {}

    class RecordingDriver(FakeDriver):
        def download_export(self, doc_id, fmt, *, product):
            seen_products[(doc_id, fmt)] = product
            return super().download_export(doc_id, fmt, product=product)

    exp = BrowserExporter(RecordingDriver(), db, tmp_path / "vault",
                          formats=["pdf", "vsdx"], delay=lambda: None)
    stats = exp.run()
    # d1 lucidchart: pdf + vsdx ok (2). d2 lucidspark: pdf ok (1), vsdx skipped (1).
    assert stats == {"ok": 3, "failed": 0, "skipped": 1}
    # product must be propagated to the driver for URL building
    assert seen_products[("d1", "pdf")] == "lucidchart"
    assert seen_products[("d2", "pdf")] == "lucidspark"
