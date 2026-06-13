"""Phase-2 artifacts via the real web UI: PDF (all products) and VSDX (Lucidchart only).

The Lucid REST API cannot export PDF/VSDX, so this module drives lucid.app with Playwright
using a persistent profile directory (.pw-profile). First run: `lucid-vault-exporter login`
opens a headed window, the user signs in manually (SSO/2FA included), and the session
persists for later headless runs.

Resilience rules: every document is its own try/except; a failure records the artifact as
failed (with a screenshot under _manifest/browser_failures/) and the loop continues; jitter
between documents keeps the pace human-ish. UI selectors live in SELECTORS so a Lucid UI
change is a one-place fix. Playwright import is lazy: API-only usage never needs it.

NOTE: the SELECTORS and the editor URL pattern are PROVISIONAL and must be validated against
the live lucid.app UI (Task 15) before a long run.
"""

from __future__ import annotations

import contextlib
import logging
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .state import StateDB
from .utils import ensure_dir, sanitize_filename

log = logging.getLogger("lucid_vault_exporter.browser")

VSDX_PRODUCTS = {"lucidchart"}

SELECTORS = {
    "file_menu": '[data-test-id="file-menu"], span:has-text("File"), span:has-text("Archivo")',
    "export_item": 'text=/Export|Exportar/i',
    "format_pdf": 'text=/PDF/i',
    "format_vsdx": 'text=/Visio|VSDX/i',
    "download_button": (
        'button:has-text("Download"), button:has-text("Descargar"),'
        ' button:has-text("Export"), button:has-text("Exportar")'
    ),
}


class BrowserDriver(Protocol):
    def download_export(self, doc_id: str, fmt: str, *, product: str) -> bytes: ...


class PlaywrightDriver:
    """Real driver. Requires ``pip install lucid-vault-exporter[browser]``
    and ``playwright install chromium``."""

    def __init__(self, profile_dir: Path, *, headless: bool = True,
                 failure_dir: Path | None = None) -> None:
        from playwright.sync_api import sync_playwright  # lazy

        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            str(profile_dir), headless=headless, accept_downloads=True,
        )
        self._failure_dir = failure_dir

    def close(self) -> None:
        self._ctx.close()
        self._pw.stop()

    def is_logged_in(self) -> bool:
        page = self._ctx.new_page()
        try:
            page.goto("https://lucid.app/documents", wait_until="domcontentloaded",
                      timeout=30000)
            page.wait_for_timeout(3000)
            return "/documents" in page.url and "login" not in page.url
        finally:
            page.close()

    def wait_for_manual_login(self, timeout_s: float = 600.0) -> None:
        page = self._ctx.new_page()
        page.goto("https://lucid.app/users/login", wait_until="domcontentloaded")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if "/documents" in page.url:
                page.close()
                return
            page.wait_for_timeout(1000)
        page.close()
        raise RuntimeError("Login was not completed within the timeout.")

    def download_export(self, doc_id: str, fmt: str, *, product: str) -> bytes:
        page = self._ctx.new_page()
        try:
            # NOTE: the per-product URL path (/{product}/) is also provisional and
            # must be validated against the live lucid.app UI (Task 15).
            page.goto(f"https://lucid.app/{product}/{doc_id}/edit",
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)  # editor boot
            page.locator(SELECTORS["file_menu"]).first.click(timeout=15000)
            page.locator(SELECTORS["export_item"]).first.click(timeout=10000)
            page.locator(SELECTORS[f"format_{fmt}"]).first.click(timeout=10000)
            with page.expect_download(timeout=120000) as dl:
                page.locator(SELECTORS["download_button"]).first.click(timeout=10000)
            path = dl.value.path()
            return Path(path).read_bytes()
        except Exception:
            if self._failure_dir:
                ensure_dir(self._failure_dir)
                with contextlib.suppress(Exception):
                    page.screenshot(path=str(self._failure_dir / f"{doc_id}-{fmt}.png"))
            raise
        finally:
            page.close()


class BrowserExporter:
    def __init__(
        self, driver: BrowserDriver, db: StateDB, vault_dir: Path,
        *, formats: list[str], delay: Callable[[], None] | None = None,
        min_delay: float = 3.0, max_delay: float = 7.0,
    ) -> None:
        self._driver = driver
        self._db = db
        self._vault = vault_dir
        self._formats = formats
        self._delay = delay or (lambda: time.sleep(random.uniform(min_delay, max_delay)))

    def run(self, progress: Callable[[str], None] | None = None) -> dict[str, int]:
        stats = {"ok": 0, "failed": 0, "skipped": 0}
        for fmt in self._formats:
            for doc in self._db.documents_missing_artifact(fmt):
                doc_id = doc["document_id"]
                if fmt == "vsdx" and doc["product"] not in VSDX_PRODUCTS:
                    self._db.set_artifact(doc_id, "vsdx", "skipped",
                                          error=f"vsdx n/a for {doc['product']}")
                    stats["skipped"] += 1
                    continue
                if progress:
                    progress(f"{fmt.upper()} {doc['title']}")
                self._db.set_artifact(doc_id, fmt, "in_progress")
                try:
                    data = self._driver.download_export(doc_id, fmt, product=doc["product"])
                except Exception as exc:  # noqa: BLE001 - any UI failure: record, continue
                    self._db.set_artifact(doc_id, fmt, "failed", error=str(exc)[:500])
                    self._db.record_error(doc_id, f"{fmt}_export", str(exc)[:500])
                    log.warning("%s export failed for %s: %s", fmt, doc["title"], exc)
                    stats["failed"] += 1
                    self._delay()
                    continue
                folder = ensure_dir(self._vault / Path(doc["folder_path"] or "."))
                name = f"{sanitize_filename(doc['title'])} {doc_id[:8]}.{fmt}"
                target = folder / name
                target.write_bytes(data)
                self._db.set_artifact(doc_id, fmt, "ok", path=str(target))
                stats["ok"] += 1
                self._delay()
        return stats
