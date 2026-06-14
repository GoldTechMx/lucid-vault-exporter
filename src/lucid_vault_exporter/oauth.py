"""OAuth2 authorization-code flow against Lucid, with a throwaway localhost callback server.

Flow (CLI `auth` command):
  1. build_authorize_url() -> open in the user's browser
  2. CallbackServer catches GET /callback?code=...&state=...
  3. exchange_code() swaps it for access+refresh tokens -> TokenStore (plain JSON, chmod 600)
  4. LucidClient asks TokenStore.access_token(); refresh() runs automatically when stale.

Scopes: readonly document content for all three products + folder:readonly + offline_access
(refresh token). Tokens are never logged.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

SCOPES = (
    "lucidchart.document.content:readonly "
    "lucidspark.document.content:readonly "
    "lucidscale.document.content:readonly "
    "folder:readonly offline_access user.profile"
)
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
_SKEW = 120  # refresh this many seconds before nominal expiry


class OAuthError(Exception):
    pass


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with contextlib.suppress(OSError):  # NTFS/SMB may not support POSIX modes
            self.path.chmod(0o600)

    def load(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        result: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        return result

    def access_token(self) -> str | None:
        data = self.load()
        if not data:
            return None
        if time.time() >= float(data.get("expires_at", 0)) - _SKEW:
            return None  # caller must refresh()
        return str(data["access_token"])

    def refresh_token(self) -> str | None:
        data = self.load()
        return str(data["refresh_token"]) if data and "refresh_token" in data else None


def build_authorize_url(auth_base: str, client_id: str, redirect_uri: str, state: str) -> str:
    params = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
        }
    )
    return f"{auth_base}/oauth2/authorize?{params}"


def _store_response(store: TokenStore, payload: dict[str, Any]) -> None:
    store.save(
        {
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token", store.refresh_token()),
            "expires_at": time.time() + float(payload.get("expires_in", 3600)),
        }
    )


def exchange_code(
    store: TokenStore,
    token_url: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> str:
    resp = httpx.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise OAuthError(f"Token exchange failed: HTTP {resp.status_code}")
    _store_response(store, resp.json())
    return str(resp.json()["access_token"])


def refresh(store: TokenStore, token_url: str, client_id: str, client_secret: str) -> str:
    rt = store.refresh_token()
    if not rt:
        raise OAuthError("No refresh token; run `lucid-vault-exporter auth` first.")
    resp = httpx.post(
        token_url,
        data={
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise OAuthError(f"Token refresh failed: HTTP {resp.status_code}. Re-run `auth`.")
    _store_response(store, resp.json())
    return str(resp.json()["access_token"])


class CallbackServer:
    """One-shot localhost HTTP server that captures ?code= from the OAuth redirect."""

    def __init__(self, expected_state: str, port: int = REDIRECT_PORT) -> None:
        self.code: str | None = None
        self.error: str | None = None
        expected = expected_state
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                q = parse_qs(urlparse(self.path).query)
                if q.get("state", [""])[0] != expected:
                    outer.error = "state mismatch"
                elif "code" in q:
                    outer.code = q["code"][0]
                else:
                    outer.error = q.get("error", ["unknown"])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    "<h2>Listo - puedes cerrar esta pestaña y volver a la terminal.</h2>".encode()
                )

            def log_message(self, *args: Any) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", port), Handler)

    def wait_for_code(self, timeout: float = 300.0) -> str:
        deadline = time.time() + timeout
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        try:
            while time.time() < deadline and self.code is None and self.error is None:
                time.sleep(0.2)
        finally:
            self._server.shutdown()
        if self.code is None:
            raise OAuthError(self.error or "Timed out waiting for OAuth redirect.")
        return self.code
