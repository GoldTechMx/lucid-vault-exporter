from pathlib import Path

import yaml

from lucid_vault_exporter.obsidian import write_note
from lucid_vault_exporter.state import StateDB


def make_doc(**over):
    doc = {
        "document_id": "d1abcdef99", "title": "Mapa: Flujo*", "product": "lucidchart",
        "folder_path": "Clientes/ACME", "page_count": 2, "created": "2020-01-01T00:00:00Z",
        "last_modified": "2021-06-01T00:00:00Z", "owner": "Boby",
        "edit_url": "https://lucid.app/lucidchart/d1abcdef99/edit", "version": "42",
        "folder_id": None, "note_path": None,
    }
    doc.update(over)
    return doc


def test_note_has_frontmatter_embeds_and_links(tmp_path: Path):
    vault = tmp_path / "vault"
    db = StateDB(tmp_path / "s.sqlite")
    keep = {k: v for k, v in make_doc().items() if v is not None or k in ("folder_id", "note_path")}
    db.upsert_document(**keep)
    note = write_note(
        db, make_doc(), vault,
        png_files=["Mapa_ Flujo_ d1abcdef p1.png", "Mapa_ Flujo_ d1abcdef p2.png"],
        sidecar_files=["Mapa_ Flujo_ d1abcdef.pdf"],
    )
    text = note.read_text(encoding="utf-8")
    assert note == vault / "Clientes" / "ACME" / "Mapa_ Flujo_.md"
    assert "lucid_id: d1abcdef99" in text
    assert "product: lucidchart" in text
    assert "![[Mapa_ Flujo_ d1abcdef p1.png]]" in text
    assert "[[Mapa_ Flujo_ d1abcdef.pdf]]" in text
    assert "https://lucid.app/lucidchart/d1abcdef99/edit" in text
    assert db.get_document("d1abcdef99")["note_path"] is not None


def test_title_collision_gets_suffix(tmp_path: Path):
    vault = tmp_path / "vault"
    db = StateDB(tmp_path / "s.sqlite")
    for doc_id in ("aaaa1111", "bbbb2222"):
        db.upsert_document(document_id=doc_id, title="Same", product="lucidchart", folder_path="")
        write_note(db, dict(db.get_document(doc_id)), vault, png_files=[], sidecar_files=[])
    names = sorted(p.name for p in vault.glob("*.md"))
    assert names == ["Same 2.md", "Same.md"]


def _frontmatter(note_text: str) -> dict:
    # extract the YAML block between the first two '---' fences and parse it
    parts = note_text.split("---", 2)
    return yaml.safe_load(parts[1])


def test_frontmatter_is_valid_yaml_with_backslash_and_quote(tmp_path):
    vault = tmp_path / "vault"
    db = StateDB(tmp_path / "s.sqlite")
    doc = make_doc(document_id="x1", title=r"Server\DB: O'Brien", owner=r"a\b'c",
                   folder_path="", note_path=None)
    note = write_note(db, doc, vault, png_files=[], sidecar_files=[])
    fm = _frontmatter(note.read_text(encoding="utf-8"))
    # the backslash and quote must round-trip EXACTLY, no TAB/escape corruption
    assert fm["title"] == r"Server\DB: O'Brien"
    assert fm["owner"] == r"a\b'c"


def test_heading_uses_raw_title_not_quoted(tmp_path):
    vault = tmp_path / "vault"
    db = StateDB(tmp_path / "s.sqlite")
    doc = make_doc(document_id="h1", title="Mapa: Flujo*", folder_path="", note_path=None)
    note = write_note(db, doc, vault, png_files=[], sidecar_files=[])
    text = note.read_text(encoding="utf-8")
    # the H1 heading must show the raw title, NOT wrapped in single quotes
    assert "\n# Mapa: Flujo*\n" in text
    assert "# 'Mapa: Flujo*'" not in text


def test_note_path_is_reused_on_rerender(tmp_path):
    vault = tmp_path / "vault"
    db = StateDB(tmp_path / "s.sqlite")
    doc = make_doc(document_id="r1", title="Reuse", folder_path="", note_path=None)
    first = write_note(db, doc, vault, png_files=[], sidecar_files=[])
    # second render reads the persisted note_path from the DB and reuses the same file
    doc2 = dict(db.get_document("r1"))
    second = write_note(db, doc2, vault, png_files=["p.png"], sidecar_files=[])
    assert first == second
    assert len(list(vault.glob("*.md"))) == 1
