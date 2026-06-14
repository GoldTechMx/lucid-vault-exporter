"""Typer CLI. Wiring only - all behavior lives in the modules it calls."""

from __future__ import annotations

import logging
import secrets
import sys
import webbrowser
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

from .config import Config, ConfigError, Settings
from .logging_setup import configure_logging
from .ratelimit import RateLimiter
from .state import StateDB

app = typer.Typer(add_completion=False, help="Export a Lucid account into an Obsidian vault.")
console = Console()
log = logging.getLogger("lucid_vault_exporter.cli")

CONFIG_PATH = Path("config.yml")
TOKENS_PATH = Path(".lucid_tokens.json")
PROFILE_DIR = Path(".pw-profile")

CONFIG_TEMPLATE = """output_dir: ./exports/lucid-vault
products: [lucidchart, lucidspark, lucidscale]
exclude_trashed: true
png_dpi: 160
browser:
  enabled: true
  formats: [pdf, vsdx]
  headless: true
  min_delay_seconds: 3.0
  max_delay_seconds: 7.0
rate_limit:
  export_per_5s: 60
  search_per_5s: 240
"""


def _load() -> tuple[Config, Settings]:
    try:
        return Config.load(CONFIG_PATH), Settings()
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _client(cfg: Config, settings: Settings) -> Any:
    from .lucid_client import LucidClient
    from .oauth import TokenStore, refresh

    store = TokenStore(TOKENS_PATH)
    token_url = f"{settings.lucid_api_base}/oauth2/token"

    def provider() -> str:
        tok = store.access_token()
        if tok:
            return tok
        return refresh(store, token_url, settings.lucid_client_id, settings.lucid_client_secret)

    rl = RateLimiter(budgets={
        "export": cfg.rate_limit.export_per_5s,
        "search": cfg.rate_limit.search_per_5s,
    })
    return LucidClient(settings.lucid_api_base, token_provider=provider, ratelimiter=rl)


@app.command()
def init() -> None:
    """Write config.yml next to you (refuses to overwrite)."""
    if CONFIG_PATH.exists():
        console.print("[red]config.yml already exists; edit it or delete it first.[/red]")
        raise typer.Exit(1)
    CONFIG_PATH.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    console.print("Wrote [bold]config.yml[/bold]. Edit output_dir, then run [bold]auth[/bold].")


@app.command()
def auth() -> None:
    """OAuth2: open browser, authorize, store tokens locally."""
    from .oauth import REDIRECT_URI, CallbackServer, TokenStore, build_authorize_url, exchange_code

    _, settings = _load()
    if not settings.lucid_client_id or not settings.lucid_client_secret:
        console.print("[red]Set LUCID_CLIENT_ID / LUCID_CLIENT_SECRET in .env first "
                      "(see README for creating the app).[/red]")
        raise typer.Exit(1)
    state = secrets.token_urlsafe(16)
    url = build_authorize_url(settings.lucid_auth_base, settings.lucid_client_id,
                              REDIRECT_URI, state)
    server = CallbackServer(state)
    console.print(f"Opening browser... if it doesn't open, visit:\n{url}")
    webbrowser.open(url)
    code = server.wait_for_code()
    exchange_code(TokenStore(TOKENS_PATH), f"{settings.lucid_api_base}/oauth2/token",
                  settings.lucid_client_id, settings.lucid_client_secret, code, REDIRECT_URI)
    console.print("[green]Authorized. Tokens stored in .lucid_tokens.json[/green]")


@app.command()
def login() -> None:
    """Open a visible browser to sign in to lucid.app; the session persists for exports."""
    from .exporter_browser import PlaywrightDriver

    driver = PlaywrightDriver(PROFILE_DIR, headless=False)
    try:
        console.print("Sign in in the browser window (SSO/2FA ok). Waiting...")
        driver.wait_for_manual_login()
        console.print("[green]Session saved to .pw-profile[/green]")
    finally:
        driver.close()


@app.command()
def export(
    skip_browser: Annotated[bool, typer.Option("--skip-browser")] = False,
    only_browser: Annotated[bool, typer.Option("--only-browser")] = False,
    force: Annotated[bool, typer.Option("--force", help="Re-export everything")] = False,
    limit: Annotated[int, typer.Option("--limit", help="Smoke test: cap both phases to N")] = 0,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run the export: Phase 1 (API: inventory+PNG+notes), Phase 2 (browser: PDF/VSDX)."""
    configure_logging(verbose)
    cfg, settings = _load()
    vault = cfg.output_dir
    with StateDB.open(vault) as db:
        if force:
            db.reset_artifacts()
        if not only_browser:
            from .pipeline import run_api_phase

            client = _client(cfg, settings)
            try:
                with Progress(TextColumn("{task.description}"), BarColumn(),
                              TextColumn("{task.completed}/{task.total}"),
                              TimeRemainingColumn(), console=console) as prog:
                    task = prog.add_task("API export", total=1)

                    def on_progress(done: int, total: int, label: str) -> None:
                        prog.update(task, completed=done, total=total,
                                    description=f"API: {label[:40]}")

                    stats = run_api_phase(client, db, vault, products=list(cfg.products),
                                          progress=on_progress, limit=limit)
            finally:
                client.close()
            console.print(f"Phase 1 done: {stats}")
        if not skip_browser and cfg.browser.enabled:
            from .exporter_browser import BrowserExporter, PlaywrightDriver
            from .pipeline import refresh_notes

            driver = PlaywrightDriver(
                PROFILE_DIR, headless=cfg.browser.headless,
                failure_dir=vault / "_manifest" / "browser_failures",
            )
            try:
                if not driver.is_logged_in():
                    console.print(
                        "[red]Not logged in. Run `lucid-vault-exporter login` first.[/red]"
                    )
                    raise typer.Exit(1)
                exporter = BrowserExporter(
                    driver, db, vault, formats=list(cfg.browser.formats),
                    min_delay=cfg.browser.min_delay_seconds,
                    max_delay=cfg.browser.max_delay_seconds,
                )
                if limit:
                    _apply_limit(db, list(cfg.browser.formats), limit)
                bstats = exporter.run(progress=lambda s: console.print(f"  {s}"))
                console.print(f"Phase 2 done: {bstats}")
            finally:
                driver.close()
            refresh_notes(db, vault)
        from .reports import write_manifest

        write_manifest(db, vault)
    console.print(f"[green]Export complete -> {vault}[/green]")


def _apply_limit(db: StateDB, formats: list[str], limit: int) -> None:
    """Smoke-test helper: mark all but the first `limit` docs per format as skipped."""
    for fmt in formats:
        for doc in db.documents_missing_artifact(fmt)[limit:]:
            db.set_artifact(doc["document_id"], fmt, "skipped", error="limited run")


@app.command()
def retry(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    """Reset failed artifacts to pending, then run export again."""
    configure_logging(verbose)
    cfg, _ = _load()
    with StateDB.open(cfg.output_dir) as db:
        for kind in ("png", "pdf", "vsdx"):
            for art in db.artifacts_by_status(kind, ("failed",)):
                db.set_artifact(art["document_id"], kind, "pending")
    console.print("Failed artifacts reset. Re-running export...")
    export(skip_browser=False, only_browser=False, force=False, limit=0, verbose=verbose)


@app.command()
def verify() -> None:
    """Recount artifact statuses and rewrite the manifest."""
    cfg, _ = _load()
    from .reports import write_manifest
    from .state import ARTIFACT_KINDS

    with StateDB.open(cfg.output_dir) as db:
        docs = db.all_documents()
        console.print(f"Documents: {len(docs)}")
        for kind in ARTIFACT_KINDS:
            ok = len(db.artifacts_by_status(kind, ("ok",)))
            failed = len(db.artifacts_by_status(kind, ("failed",)))
            skipped = len(db.artifacts_by_status(kind, ("skipped",)))
            console.print(f"  {kind}: ok={ok} failed={failed} skipped={skipped} "
                          f"missing={len(docs) - ok - failed - skipped}")
        write_manifest(db, cfg.output_dir)
    console.print("Manifest refreshed under _manifest/.")


@app.command()
def serve(port: Annotated[int, typer.Option("--port")] = 8123) -> None:
    """Local web UI (requires `pip install lucid-vault-exporter[web]`)."""
    try:
        import uvicorn

        from .web import create_app
    except ImportError as exc:
        console.print("[red]Install the web extra: pip install 'lucid-vault-exporter[web]'[/red]")
        raise typer.Exit(1) from exc
    uvicorn.run(create_app(), host="127.0.0.1", port=port)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted - state saved; re-run to resume.[/yellow]")
        sys.exit(130)
