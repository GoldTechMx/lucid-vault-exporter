"""Local web UI for Lucid Vault Exporter.

A FastAPI app + a single embedded HTML page so a non-CLI user can connect to Lucid (OAuth2),
optionally sign in for the browser phase, pick an output folder, choose what to export, and run
it with live progress, ETA, pause/resume/cancel, and an event console - without the terminal.

Safety: binds 127.0.0.1 only (set by `serve`); OAuth tokens live in `.lucid_tokens.json` and the
client secret in `.env` only if "remember" is set (both gitignored); the event console scrubs
secrets. StateDB is opened per request/job thread (it is not thread-safe).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .config import Settings
from .jobs import JobLogHandler, JobRegistry, RedactionFilter

log = logging.getLogger("lucid_vault_exporter.web")

TOKENS_PATH = Path(".lucid_tokens.json")
PROFILE_DIR = Path(".pw-profile")

# --- in-memory server state (single local user) -----------------------------------------
_redaction = RedactionFilter()
_registry = JobRegistry()
# connection state for the OAuth flow
_conn: dict[str, Any] = {
    "client_id": "", "client_secret": "", "state": "", "status": "idle",  # idle|pending|connected|error
    "user": None, "error": None, "server": None,
}
# browser-login state
_browser: dict[str, Any] = {"status": "idle", "error": None}  # idle|pending|logged_in|error


def _install_logging() -> None:
    handler = JobLogHandler(_registry)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_redaction)
    root = logging.getLogger()
    root.addFilter(_redaction)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _write_env(client_id: str, client_secret: str) -> None:
    """Persist client_id/secret to .env (the sanctioned, gitignored secret location)."""
    env = Path(".env")
    keep: list[str] = []
    if env.exists():
        keep = [
            ln for ln in env.read_text(encoding="utf-8").splitlines()
            if not ln.startswith(("LUCID_CLIENT_ID=", "LUCID_CLIENT_SECRET="))
        ]
    keep.append(f"LUCID_CLIENT_ID={client_id}")
    keep.append(f"LUCID_CLIENT_SECRET={client_secret}")
    env.write_text("\n".join(keep) + "\n", encoding="utf-8")


def _browse(raw: str | None) -> dict[str, Any]:
    """List immediate subfolders of `raw` on the SERVER filesystem (for the picker).
    Localhost-only by design; returns directory names only and never crashes on IO errors."""
    base = Path(raw).expanduser() if raw else Path.home()
    try:
        base = base.resolve()
        if not base.is_dir():
            base = Path.home().resolve()
    except OSError:
        base = Path.home().resolve()
    dirs: list[dict[str, str]] = []
    try:
        for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            try:
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry)})
            except OSError:
                continue
    except (PermissionError, OSError):
        pass
    parent = str(base.parent) if base.parent != base else None
    return {"path": str(base), "parent": parent, "writable": os.access(base, os.W_OK),
            "dirs": dirs[:1000]}


def _check_path(raw: str) -> dict[str, Any]:
    """Resolve a typed output path and report whether it's usable (live UI feedback)."""
    if not raw.strip():
        return {"ok": False, "abs": "", "msg": "enter a folder path"}
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError:
        return {"ok": False, "abs": raw, "msg": "invalid path"}
    exists = p.exists()
    probe = p if exists else next((a for a in p.parents if a.exists()), p)
    writable = os.access(probe, os.W_OK)
    if not writable:
        msg = "not writable"
    elif exists:
        msg = "exists - writable"
    else:
        msg = "will be created"
    return {"ok": writable, "abs": str(p), "exists": exists, "writable": writable, "msg": msg}


def _load_env_credentials() -> None:
    """Pick up client_id/secret already in .env so the UI can pre-fill and auto-connect later."""
    s = Settings()
    if s.lucid_client_id and s.lucid_client_secret:
        _conn["client_id"] = s.lucid_client_id
        _conn["client_secret"] = s.lucid_client_secret
        _redaction.set_secrets([s.lucid_client_secret])


def create_app() -> Any:
    raise NotImplementedError  # replaced in Task 7
