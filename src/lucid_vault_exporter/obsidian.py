"""Obsidian note per document: YAML frontmatter + PNG embeds + sidecar links.

Layout per document (inside the mirrored Lucid folder tree):
    <Folder>/<Title>.md            <- the note (collision-suffixed 'Title 2.md', ...)
    <Folder>/_assets/<...>.png     <- page images (written by exporter_api)
    <Folder>/<Title> <id8>.pdf     <- browser sidecars, embedded by name
Obsidian resolves ![[name.png]] vault-wide by filename, so embeds use bare names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .state import _DOC_COLUMNS, StateDB
from .utils import ensure_dir, sanitize_filename, unique_path


def _yaml_sq(value: str) -> str:
    """Render a string as a YAML single-quoted scalar (literal; no escape processing).
    Internal single-quotes are doubled per the YAML spec."""
    return "'" + value.replace("'", "''") + "'"


_NOTE_TEMPLATE = """---
lucid_id: {doc_id}
title: {title}
product: {product}
folder: {folder}
pages: {pages}
created: {created}
last_modified: {last_modified}
owner: {owner}
version: {version}
original_url: {url}
exported_by: lucid-vault-exporter
---

# {title_raw}

> Diagrama exportado de Lucid ({product}). Original: {url}

{embeds}
{sidecars}
"""


def write_note(
    db: StateDB, doc: dict[str, Any], vault_dir: Path,
    *, png_files: list[str], sidecar_files: list[str],
) -> Path:
    folder = ensure_dir(vault_dir / Path(doc["folder_path"] or "."))
    existing = doc.get("note_path")
    note = Path(existing) if existing else unique_path(
        folder / f"{sanitize_filename(doc['title'])}.md"
    )
    embeds = "\n\n".join(f"![[{name}]]" for name in png_files) or "_(sin páginas exportadas)_"
    sidecars = (
        "\n\n## Archivos\n" + "\n".join(f"- [[{name}]]" for name in sidecar_files)
        if sidecar_files else ""
    )
    note.write_text(
        _NOTE_TEMPLATE.format(
            doc_id=doc["document_id"],
            title=_yaml_sq(doc["title"]),
            title_raw=doc["title"],
            product=doc["product"],
            folder=_yaml_sq(doc["folder_path"] or "/"),
            pages=doc.get("page_count") or len(png_files) or 0,
            created=doc.get("created") or "unknown",
            last_modified=doc.get("last_modified") or "unknown",
            owner=_yaml_sq(doc.get("owner") or "unknown"),
            version=_yaml_sq(doc.get("version") or ""),
            url=doc.get("edit_url") or f"https://lucid.app/documents/{doc['document_id']}",
            embeds=embeds,
            sidecars=sidecars,
        ),
        encoding="utf-8",
    )
    # Persist all valid document fields (so a subsequent db.get_document returns the full record)
    # then ensure note_path is up-to-date.
    storable = {k: v for k, v in doc.items() if k in _DOC_COLUMNS}
    storable["note_path"] = str(note)
    db.upsert_document(**storable)
    return note
