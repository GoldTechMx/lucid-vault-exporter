"""SQLite-backed export state: per document x artifact (png/pdf/vsdx) status.

Mirrors quip-vault-exporter's StateDB conventions: WAL + synchronous=NORMAL (thousands of
tiny commits on an SMB share), explicit status transitions, column allowlist on upserts.
Resume rule: anything not 'ok' is re-attempted; `--force` resets artifacts to pending.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

log = logging.getLogger("lucid_vault_exporter.state")

STATE_FILENAME = "_lucid_export_state.sqlite"


def _local_state_path(output_dir: Path) -> Path:
    """A stable local-disk location for the state DB, keyed to the output dir. Used when the
    output dir is on a filesystem SQLite can't run on (some SMB/NFS shares). Deterministic so
    every command (export, verify, retry) resolves to the same state for the same output dir."""
    base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or Path.home())
    key = hashlib.sha1(str(output_dir).lower().encode("utf-8")).hexdigest()[:16]
    return base / "lucid-vault-exporter" / "state" / f"{key}.sqlite"
ARTIFACT_KINDS = ("png", "pdf", "vsdx")
VALID_STATUSES = ("pending", "in_progress", "ok", "failed", "skipped")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    document_id   TEXT PRIMARY KEY,
    title         TEXT,
    product       TEXT,
    folder_id     TEXT,
    folder_path   TEXT,
    page_count    INTEGER,
    version       TEXT,
    created       TEXT,
    last_modified TEXT,
    owner         TEXT,
    edit_url      TEXT,
    note_path     TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    document_id TEXT NOT NULL,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    path        TEXT,
    error       TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (document_id, kind)
);

CREATE TABLE IF NOT EXISTS folders (
    folder_id  TEXT PRIMARY KEY,
    title      TEXT,
    parent_id  TEXT,
    path       TEXT
);

CREATE TABLE IF NOT EXISTS errors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id       TEXT,
    operation     TEXT,
    error_message TEXT,
    timestamp     TEXT DEFAULT (datetime('now'))
);
"""

_DOC_COLUMNS = frozenset({
    "document_id", "title", "product", "folder_id", "folder_path", "page_count",
    "version", "created", "last_modified", "owner", "edit_url", "note_path",
})


class StateDB:
    """SQLite state store. NOT thread-safe: sqlite3 connections use check_same_thread=True,
    so each thread (e.g. the web UI worker) must open its OWN StateDB via StateDB.open()."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        try:
            self._conn = self._connect(wal=True)
        except sqlite3.OperationalError as exc:
            # WAL needs shared-memory (-wal/-shm) files, which network filesystems (SMB/NFS)
            # frequently cannot provide -> "disk I/O error". Fall back to the portable
            # rollback journal so an export to a mapped/UNC drive still works (a bit slower).
            log.warning("WAL journal unavailable (%s); falling back to DELETE journal mode.", exc)
            self._conn = self._connect(wal=False)

    def _connect(self, *, wal: bool) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path))
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA journal_mode={'WAL' if wal else 'DELETE'}")
            conn.execute(f"PRAGMA synchronous={'NORMAL' if wal else 'FULL'}")
            conn.executescript(_SCHEMA)
            conn.commit()
        except sqlite3.OperationalError:
            conn.close()
            raise
        return conn

    @classmethod
    def open(cls, output_dir: Path) -> StateDB:
        primary = Path(output_dir) / STATE_FILENAME
        try:
            return cls(primary)
        except sqlite3.OperationalError:
            # SQLite can't operate on this filesystem at all (some network shares fail even the
            # DELETE journal). Keep state on local disk; vault files still write to output_dir.
            local = _local_state_path(Path(output_dir))
            log.warning(
                "SQLite state cannot run under %s (network share?); keeping state on local disk "
                "at %s. The exported vault still goes to %s.",
                primary.parent, local, output_dir,
            )
            return cls(local)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> StateDB:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- documents -------------------------------------------------------------------------
    def upsert_document(self, **fields: Any) -> None:
        if "document_id" not in fields:
            raise ValueError("upsert_document requires document_id")
        unknown = set(fields) - _DOC_COLUMNS
        if unknown:
            raise ValueError(f"unknown column(s): {sorted(unknown)}")
        cols = list(fields)
        placeholders = ", ".join(f":{c}" for c in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "document_id")
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                f"INSERT INTO documents ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(document_id) DO UPDATE SET {updates}",
                fields,
            )
        self._conn.commit()

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE document_id=?", (document_id,)
        ).fetchone()
        return dict(row) if row else None

    def all_documents(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM documents ORDER BY folder_path, title")]

    # -- artifacts -------------------------------------------------------------------------
    def set_artifact(
        self, document_id: str, kind: str, status: str,
        *, path: str | None = None, error: str | None = None,
    ) -> None:
        if kind not in ARTIFACT_KINDS:
            raise ValueError(f"invalid artifact kind {kind!r}")
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}")
        # retry_count counts FAILURES (first failure -> 1), not retries-after-first.
        bump = 1 if status == "failed" else 0
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO artifacts (document_id, kind, status, path, error, retry_count) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(document_id, kind) DO UPDATE SET status=excluded.status, "
                "path=COALESCE(excluded.path, artifacts.path), error=excluded.error, "
                "retry_count=artifacts.retry_count+?, updated_at=datetime('now')",
                (document_id, kind, status, path, error, bump, bump),
            )
        self._conn.commit()

    def get_artifact(self, document_id: str, kind: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE document_id=? AND kind=?", (document_id, kind)
        ).fetchone()
        return dict(row) if row else None

    def artifacts_by_status(self, kind: str, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT * FROM artifacts WHERE kind=? AND status IN ({marks})",
            (kind, *statuses),
        ).fetchall()
        return [dict(r) for r in rows]

    def documents_missing_artifact(self, kind: str) -> list[dict[str, Any]]:
        """Documents with no 'ok'/'skipped' row for this artifact kind (= pending work)."""
        rows = self._conn.execute(
            "SELECT d.* FROM documents d LEFT JOIN artifacts a "
            "ON a.document_id=d.document_id AND a.kind=? "
            "WHERE a.status IS NULL OR a.status NOT IN ('ok','skipped') "
            "ORDER BY d.folder_path, d.title",
            (kind,),
        ).fetchall()
        return [dict(r) for r in rows]

    def reset_artifacts(self, kind: str | None = None) -> None:
        """--force support: forget completion so everything re-exports."""
        if kind:
            self._conn.execute("UPDATE artifacts SET status='pending' WHERE kind=?", (kind,))
        else:
            self._conn.execute("UPDATE artifacts SET status='pending'")
        self._conn.commit()

    # -- folders / errors ------------------------------------------------------------------
    def upsert_folder(self, folder_id: str, title: str, parent_id: str | None, path: str) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO folders (folder_id, title, parent_id, path) VALUES (?,?,?,?) "
                "ON CONFLICT(folder_id) DO UPDATE SET title=excluded.title, "
                "parent_id=excluded.parent_id, path=excluded.path",
                (folder_id, title, parent_id, path),
            )
        self._conn.commit()

    def get_folder(self, folder_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM folders WHERE folder_id=?", (folder_id,)
        ).fetchone()
        return dict(row) if row else None

    def record_error(self, item_id: str, operation: str, message: str) -> None:
        self._conn.execute(
            "INSERT INTO errors (item_id, operation, error_message) VALUES (?,?,?)",
            (item_id, operation, message),
        )
        self._conn.commit()

    def all_errors(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM errors")]
