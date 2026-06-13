"""Phase-1 artifact: one PNG per document page, written under <folder>/_assets/.

Page discovery: trust page_count from inventory when present; otherwise request pages
1, 2, 3... until PageNotFound (404). Files are named '<title> <docid> p<N>.png' - the doc
id guarantees uniqueness across same-titled documents, and the name is what Obsidian
embeds reference. Any LucidError marks the png artifact failed and the run continues.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol

from .lucid_client import LucidError, PageNotFound
from .state import StateDB
from .utils import ensure_dir, sanitize_filename

log = logging.getLogger("lucid_vault_exporter.exporter_api")

_MAX_PAGES = 500  # hard stop for discovery mode


class PngClient(Protocol):
    def export_page_png(self, doc_id: str, *, page: int) -> bytes: ...


def png_filename(title: str, doc_id: str, page: int) -> str:
    return f"{sanitize_filename(title)} {doc_id[:8]} p{page}.png"


def export_document_pngs(
    client: PngClient, db: StateDB, doc: dict[str, Any], vault_dir: Path,
) -> list[Path]:
    doc_id = doc["document_id"]
    assets = ensure_dir(vault_dir / Path(doc["folder_path"] or ".") / "_assets")
    db.set_artifact(doc_id, "png", "in_progress")
    written: list[Path] = []
    page_count = doc.get("page_count")
    try:
        page = 1
        while True:
            if page_count and page > int(page_count):
                break
            if page > _MAX_PAGES:
                log.warning("%s: stopping page discovery at %d pages.", doc_id, _MAX_PAGES)
                break
            try:
                data = client.export_page_png(doc_id, page=page)
            except PageNotFound:
                break
            target = assets / png_filename(doc["title"], doc_id, page)
            target.write_bytes(data)
            written.append(target)
            page += 1
    except LucidError as exc:
        db.set_artifact(doc_id, "png", "failed", error=str(exc))
        db.record_error(doc_id, "png_export", str(exc))
        log.warning("PNG export failed for %s (%s): %s", doc["title"], doc_id, exc)
        return []
    if not written:
        db.set_artifact(doc_id, "png", "failed", error="no pages exported")
        return []
    db.set_artifact(doc_id, "png", "ok", path=str(written[0].parent))
    return written
