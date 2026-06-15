"""Orchestration: Phase 1 (API) = inventory -> PNGs -> notes; Phase 2 (browser) lives in
exporter_browser and is invoked by the CLI. Both phases resume from StateDB. A progress
callback (used by CLI rich-progress and the web UI) receives (done, total, label)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .control import Control
from .exporter_api import export_document_pngs
from .inventory import run_inventory
from .obsidian import write_note
from .state import StateDB

log = logging.getLogger("lucid_vault_exporter.pipeline")

ProgressFn = Callable[[int, int, str], None]


def run_api_phase(
    client: Any, db: StateDB, vault_dir: Path,
    *, products: list[str] | None = None, progress: ProgressFn | None = None,
    control: Control | None = None, limit: int = 0,
) -> dict[str, int]:
    products = products or ["lucidchart", "lucidspark", "lucidscale"]
    n_docs = run_inventory(client, db, products=products, control=control)
    todo = db.documents_missing_artifact("png")
    # `limit` caps the work to the first N pending documents (smoke test); 0 = no cap.
    if limit:
        todo = todo[:limit]
    stats = {"documents": n_docs, "png_ok": 0, "png_failed": 0}
    total = len(todo)
    for i, doc in enumerate(todo, 1):
        if control:
            control.checkpoint()  # blocks while paused; raises Cancelled on cancel
        if progress:
            progress(i, total, doc["title"])
        pngs = export_document_pngs(client, db, doc, vault_dir)
        if pngs:
            stats["png_ok"] += 1
        else:
            stats["png_failed"] += 1
        sidecars = [
            Path(a["path"]).name
            for kind in ("pdf", "vsdx")
            if (a := db.get_artifact(doc["document_id"], kind))
            and a["status"] == "ok" and a["path"]
        ]
        write_note(db, doc, vault_dir,
                   png_files=[p.name for p in pngs], sidecar_files=sidecars)
    return stats


def refresh_notes(db: StateDB, vault_dir: Path) -> int:
    """After the browser phase, re-render notes so sidecar links appear."""
    count = 0
    for doc in db.all_documents():
        png_art = db.get_artifact(doc["document_id"], "png")
        png_dir = Path(png_art["path"]) if png_art and png_art.get("path") else None
        pattern = f"*{doc['document_id'][:8]}*.png"
        pngs = (
            sorted(p.name for p in png_dir.glob(pattern))
            if png_dir and png_dir.is_dir() else []
        )
        sidecars = [
            Path(a["path"]).name
            for kind in ("pdf", "vsdx")
            if (a := db.get_artifact(doc["document_id"], kind))
            and a["status"] == "ok" and a["path"]
        ]
        write_note(db, doc, vault_dir, png_files=pngs, sidecar_files=sidecars)
        count += 1
    return count
