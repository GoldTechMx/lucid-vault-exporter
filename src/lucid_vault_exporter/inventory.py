"""Inventory: enumerate every readable document and resolve its folder path.

Strategy: documents/search is the single source of truth for the document list (it returns
everything the user can read, owned or shared). Folder PATHS are resolved lazily by walking
each document's `parent` chain upward via get_folder(), memoised in StateDB.folders.
Cycle-safe: a walk aborts at 50 hops or on a repeated id and keeps the partial path.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from .state import StateDB
from .utils import sanitize_filename

log = logging.getLogger("lucid_vault_exporter.inventory")

_MAX_DEPTH = 50


class InventoryClient(Protocol):
    def search_documents(self, **kw: Any) -> Any: ...
    def get_folder(self, folder_id: str) -> dict[str, Any] | None: ...


def _folder_path(client: InventoryClient, db: StateDB, folder_id: str | None) -> str:
    if not folder_id:
        return ""
    cached = db.get_folder(folder_id)
    if cached and cached.get("path") is not None:
        return str(cached["path"])
    # Walk upward collecting uncached folders (bottom-up) until root, a cycle, the depth
    # cap, or a cached ancestor whose full path becomes our prefix.
    chain: list[tuple[str, str, str | None]] = []  # (id, name, parent), bottom-up
    seen: set[str] = set()
    prefix = ""  # full path of the first cached ancestor encountered, if any
    current: str | None = folder_id
    while current and current not in seen and len(chain) < _MAX_DEPTH:
        seen.add(current)
        cached = db.get_folder(current)
        if cached and cached.get("path") is not None:
            prefix = str(cached["path"])
            break
        folder = client.get_folder(current)
        if folder is None:
            break
        name = sanitize_filename(str(folder.get("name") or folder.get("title") or current))
        parent = folder.get("parent") or folder.get("parentId")
        chain.append((current, name, str(parent) if parent else None))
        current = str(parent) if parent else None
    # chain is bottom-up; build cumulative paths top-down, starting from the cached prefix.
    cumulative = prefix
    for fid, name, parent in reversed(chain):
        cumulative = f"{cumulative}/{name}" if cumulative else name
        db.upsert_folder(fid, name, parent, cumulative)
    return cumulative


def run_inventory(client: InventoryClient, db: StateDB, *, products: list[str]) -> int:
    count = 0
    for doc in client.search_documents(products=products, exclude_trashed=True):
        doc_id = str(doc.get("documentId") or doc.get("id"))
        folder_id = doc.get("parent") or doc.get("folderId")
        owner = doc.get("owner")
        if isinstance(owner, dict):
            owner = owner.get("name") or owner.get("id")
        db.upsert_document(
            document_id=doc_id,
            title=str(doc.get("title") or "untitled"),
            product=str(doc.get("product") or "lucidchart"),
            folder_id=str(folder_id) if folder_id else None,
            folder_path=_folder_path(client, db, str(folder_id) if folder_id else None),
            page_count=doc.get("pageCount"),
            version=str(doc.get("version") or ""),
            created=doc.get("created"),
            last_modified=doc.get("lastModified"),
            owner=str(owner) if owner else None,
            edit_url=doc.get("editUrl"),
        )
        count += 1
        if count % 100 == 0:
            log.info("Inventory: %d documents so far...", count)
    log.info("Inventory complete: %d documents.", count)
    return count
