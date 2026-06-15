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
import secrets as _secrets
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__
from .config import VALID_PRODUCTS, Config, ConfigError, Settings
from .control import Cancelled
from .jobs import JobLogHandler, JobRegistry, RedactionFilter, scrub_text
from .ratelimit import RateLimiter
from .state import ARTIFACT_KINDS, StateDB

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


_PAGE = ("<!doctype html><html><head><meta charset='utf-8'><title>Lucid Vault Exporter</title>"
         "</head><body><h1>Lucid Vault Exporter</h1><p>UI loads in Task 8.</p></body></html>")


def _settings() -> Settings:
    return Settings()


def _make_client(cfg: Config) -> Any:
    """Build a LucidClient from the stored OAuth token (refreshing if needed)."""
    from .lucid_client import LucidClient
    from .oauth import TokenStore, refresh

    s = _settings()
    store = TokenStore(TOKENS_PATH)
    token_url = f"{s.lucid_api_base}/oauth2/token"
    # prefer the live connection's credentials (may not be in .env unless 'remember' was set)
    cid = _conn.get("client_id") or s.lucid_client_id
    csec = _conn.get("client_secret") or s.lucid_client_secret

    def provider() -> str:
        tok = store.access_token()
        if tok:
            return tok
        return refresh(store, token_url, cid, csec)

    rl = RateLimiter(budgets={"export": cfg.rate_limit.export_per_5s,
                              "search": cfg.rate_limit.search_per_5s})
    return LucidClient(s.lucid_api_base, token_provider=provider, ratelimiter=rl)


def _start_oauth_capture(state: str) -> None:
    """Start a CallbackServer thread that captures the OAuth code and exchanges it for tokens.
    Updates _conn['status'] to connected/error. Stubbed in tests."""
    from .oauth import REDIRECT_URI, CallbackServer, TokenStore, exchange_code

    s = _settings()

    def worker() -> None:
        try:
            server = CallbackServer(state)
            code = server.wait_for_code(timeout=300.0)
            exchange_code(TokenStore(TOKENS_PATH), f"{s.lucid_api_base}/oauth2/token",
                          _conn["client_id"], _conn["client_secret"], code, REDIRECT_URI)
            _conn["status"] = "connected"
            _conn["user"] = "Lucid user"
            log.info("Connected to Lucid.")
        except Exception as exc:  # noqa: BLE001 - surface to the UI, never crash
            _conn["status"] = "error"
            _conn["error"] = scrub_text(str(exc), [_conn["client_secret"]])
            log.warning("OAuth connect failed: %s", _conn["error"])

    threading.Thread(target=worker, daemon=True).start()


def _start_browser_login() -> None:
    """Launch a headed Playwright login in a thread; update _browser status."""
    def worker() -> None:
        try:
            from .exporter_browser import PlaywrightDriver
            driver = PlaywrightDriver(PROFILE_DIR, headless=False)
            try:
                driver.wait_for_manual_login()
                _browser["status"] = "logged_in"
                log.info("Browser session saved for PDF/VSDX.")
            finally:
                driver.close()
        except Exception as exc:  # noqa: BLE001
            _browser["status"] = "error"
            _browser["error"] = str(exc)
            log.warning("Browser login failed: %s", exc)

    _browser["status"] = "pending"
    _browser["error"] = None
    threading.Thread(target=worker, daemon=True).start()


def _browser_logged_in() -> bool:
    """A persisted profile means a prior login is likely valid; status='logged_in' confirms."""
    if _browser["status"] == "logged_in":
        return True
    return PROFILE_DIR.exists()


def _build_config(opts: dict[str, Any]) -> Config:
    cfg = Config.load(Path("config.yml"))
    if opts.get("output_dir"):
        cfg.output_dir = Path(opts["output_dir"])
    if opts.get("products"):
        bad = [p for p in opts["products"] if p not in VALID_PRODUCTS]
        if bad:
            raise ConfigError(f"unknown product(s): {bad}")
        cfg.products = list(opts["products"])
    return cfg


def _run_job(job: Any, cfg: Config, command: str, options: dict[str, Any]) -> None:
    from .exporter_browser import BrowserExporter, PlaywrightDriver
    from .pipeline import refresh_notes, run_api_phase
    from .reports import write_manifest

    _redaction.set_secrets([_conn.get("client_secret") or ""])
    job.started = job.phase_started = time.monotonic()
    vault = cfg.output_dir
    limit = int(options.get("limit") or 0)
    try:
        with StateDB.open(vault) as db:
            if options.get("force"):
                db.reset_artifacts()
            if command in ("export", "export_api", "inventory"):
                client = _make_client(cfg)
                try:
                    if command == "inventory":
                        from .inventory import run_inventory
                        job.progress("inventory", 0, None, "scanning")
                        n = run_inventory(client, db, products=list(cfg.products),
                                          control=job.control)
                        job.result = {"documents": n}
                    else:
                        def on_progress(done: int, total: int, label: str) -> None:
                            job.progress("API export", done, total, label)
                        stats = run_api_phase(client, db, vault, products=list(cfg.products),
                                              progress=on_progress, control=job.control,
                                              limit=limit)
                        job.result = dict(stats)
                finally:
                    client.close()
            if command in ("export", "export_browser") and cfg.browser.enabled:
                driver = PlaywrightDriver(PROFILE_DIR, headless=cfg.browser.headless,
                                          failure_dir=vault / "_manifest" / "browser_failures")
                try:
                    exporter = BrowserExporter(
                        driver, db, vault, formats=list(cfg.browser.formats),
                        min_delay=cfg.browser.min_delay_seconds,
                        max_delay=cfg.browser.max_delay_seconds, control=job.control)

                    def bprog(label: str) -> None:
                        job.progress("Browser export", job.done + 1, job.total, label)

                    bstats = exporter.run(progress=bprog)
                    job.result = {**(job.result or {}), "browser": bstats}
                finally:
                    driver.close()
                refresh_notes(db, vault)
            write_manifest(db, vault)
        job.status = "done"
        log.info("%s complete.", command)
    except Cancelled:
        job.status = "cancelled"
        log.info("%s cancelled. Re-run to continue where it left off.", command)
    except Exception as exc:  # noqa: BLE001 - never crash the server
        job.error = scrub_text(str(exc), [_conn.get("client_secret") or ""])
        job.status = "error"
        log.error("%s failed: %s", command, job.error)
    finally:
        _registry.active = None


def create_app() -> Any:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse

    _install_logging()
    _load_env_credentials()
    app = FastAPI(title="Lucid Vault Exporter", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> Any:
        return HTMLResponse(_PAGE.replace("__LVE_VERSION__", __version__),
                            headers={"Cache-Control": "no-store, max-age=0"})

    @app.get("/api/status")
    def status() -> JSONResponse:
        try:
            cfg = Config.load(Path("config.yml"))
            output_dir = str(cfg.output_dir)
            counts = {k: dict.fromkeys(("ok", "failed", "skipped"), 0) for k in ARTIFACT_KINDS}
            documents = 0
            with StateDB.open(cfg.output_dir) as db:
                documents = len(db.all_documents())
                for k in ARTIFACT_KINDS:
                    for stt in ("ok", "failed", "skipped"):
                        counts[k][stt] = len(db.artifacts_by_status(k, (stt,)))
        except ConfigError:
            output_dir, documents = "", 0
            counts = {k: dict.fromkeys(("ok", "failed", "skipped"), 0) for k in ARTIFACT_KINDS}
        job = _registry.active
        return JSONResponse({
            "connected": _conn["status"] == "connected",
            "user": _conn["user"],
            "browser_logged_in": _browser_logged_in(),
            "output_dir": output_dir,
            "documents": documents,
            "artifacts": counts,
            "active_job_id": job.id if job else None,
        })

    @app.post("/api/connect")
    def connect(body: dict[str, Any]) -> JSONResponse:
        from .oauth import REDIRECT_URI, build_authorize_url

        cid = (body.get("client_id") or "").strip()
        sec = (body.get("client_secret") or "").strip()
        if not cid or not sec:
            raise HTTPException(status_code=400, detail="Client ID and Client Secret are required.")
        state = _secrets.token_urlsafe(16)
        _conn.update({"client_id": cid, "client_secret": sec, "state": state,
                      "status": "pending", "user": None, "error": None})
        _redaction.set_secrets([sec])
        if body.get("remember"):
            _write_env(cid, sec)
        s = _settings()
        url = build_authorize_url(s.lucid_auth_base, cid, REDIRECT_URI, state)
        _start_oauth_capture(state)
        return JSONResponse({"authorize_url": url, "state": state})

    @app.get("/api/connect/poll")
    def connect_poll() -> JSONResponse:
        return JSONResponse({"state": _conn["status"], "user": _conn["user"],
                             "error": _conn["error"]})

    @app.post("/api/browser-login")
    def browser_login() -> JSONResponse:
        _start_browser_login()
        return JSONResponse({"ok": True})

    @app.get("/api/browser-login/poll")
    def browser_login_poll() -> JSONResponse:
        return JSONResponse({"state": _browser["status"], "error": _browser["error"]})

    @app.get("/api/browse")
    def browse(path: str | None = None) -> JSONResponse:
        return JSONResponse(_browse(path))

    @app.get("/api/check-path")
    def check_path(path: str = "") -> JSONResponse:
        return JSONResponse(_check_path(path))

    @app.post("/api/run")
    def run(body: dict[str, Any]) -> JSONResponse:
        command = body.get("command")
        if command not in ("export", "export_api", "export_browser", "inventory", "verify"):
            raise HTTPException(status_code=400, detail="Unknown command.")
        if _conn["status"] != "connected" and command != "verify":
            raise HTTPException(status_code=400, detail="Connect to Lucid first.")
        if command in ("export", "export_browser") and not _browser_logged_in():
            raise HTTPException(status_code=400,
                                detail="Sign in for PDF/VSDX first (browser login).")
        options = body.get("options", {})
        try:
            cfg = _build_config(options)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        chk = _check_path(str(cfg.output_dir))
        if not chk["ok"]:
            raise HTTPException(status_code=400, detail=f"Output folder: {chk['msg']}")
        if command == "verify":
            from .reports import write_manifest
            with StateDB.open(cfg.output_dir) as db:
                write_manifest(db, cfg.output_dir)
            return JSONResponse({"job_id": None, "verified": True})
        if _registry.active is not None and _registry.active.status == "running":
            raise HTTPException(status_code=409, detail="A job is already running.")
        job = _registry.create(command)
        _registry.active = job
        threading.Thread(target=_run_job, args=(job, cfg, command, options), daemon=True).start()
        return JSONResponse({"job_id": job.id})

    @app.post("/api/jobs/{job_id}/{action}")
    def control_job(job_id: str, action: str) -> JSONResponse:
        job = _registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such job.")
        if action == "pause":
            job.control.pause()
        elif action == "resume":
            job.control.resume()
        elif action == "cancel":
            job.control.cancel()
        else:
            raise HTTPException(status_code=400, detail="Unknown action.")
        return JSONResponse({"ok": True, "paused": job.control.is_paused})

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str, since: int = 0) -> JSONResponse:
        job = _registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such job.")
        now = time.monotonic()
        elapsed = now - job.started if job.started else 0.0
        eta = None
        if job.total and job.done > 0 and job.status == "running":
            per_item = (now - job.phase_started) / job.done
            eta = max(0.0, (job.total - job.done) * per_item)
        return JSONResponse({
            "status": job.status, "command": job.command, "phase": job.phase,
            "paused": job.control.is_paused, "done": job.done, "total": job.total,
            "detail": job.detail, "elapsed": round(elapsed, 1),
            "eta": None if eta is None else round(eta, 1),
            "result": job.result, "error": job.error,
            "logs": job.logs[max(0, since - job.log_dropped):],
            "log_count": job.log_dropped + len(job.logs),
        })

    @app.get("/favicon.ico")
    def favicon() -> Any:
        from fastapi.responses import Response
        return Response(status_code=204)

    return app


def serve(host: str = "127.0.0.1", port: int = 8123) -> None:
    import uvicorn
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
