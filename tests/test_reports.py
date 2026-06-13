from pathlib import Path

from lucid_vault_exporter.reports import write_manifest
from lucid_vault_exporter.state import StateDB


def test_manifest_files_written(tmp_path: Path):
    vault = tmp_path / "vault"
    with StateDB.open(vault) as db:
        db.upsert_document(document_id="d1", title="T", product="lucidchart", folder_path="")
        db.set_artifact("d1", "png", "ok")
        db.set_artifact("d1", "pdf", "failed", error="boom")
        write_manifest(db, vault)
    man = vault / "_manifest"
    assert (man / "inventory.csv").is_file()
    assert (man / "errors.csv").is_file()
    assert (man / "verification.md").is_file()
    assert (man / "cancellation-checklist.md").is_file()
    verification = (man / "verification.md").read_text(encoding="utf-8")
    assert "pdf" in verification and "failed: 1" in verification


def test_inventory_csv_has_rows_and_missing_token(tmp_path: Path):
    import csv as _csv
    vault = tmp_path / "vault"
    with StateDB.open(vault) as db:
        db.upsert_document(document_id="d1", title="T", product="lucidchart", folder_path="")
        db.set_artifact("d1", "png", "ok")
        # no pdf/vsdx rows -> should show "missing"
        write_manifest(db, vault)
    rows = list(_csv.DictReader((vault / "_manifest" / "inventory.csv").open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["document_id"] == "d1"
    assert rows[0]["png_status"] == "ok"
    assert rows[0]["vsdx_status"] == "missing"
