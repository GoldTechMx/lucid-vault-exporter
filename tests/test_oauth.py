import json
import time
from pathlib import Path

from lucid_vault_exporter.oauth import TokenStore, build_authorize_url, exchange_code, refresh

TOKEN_URL = "https://api.lucid.co/oauth2/token"


def test_authorize_url_contains_required_params():
    url = build_authorize_url("https://lucid.app", "cid", "http://localhost:8765/callback", "st8")
    assert url.startswith("https://lucid.app/oauth2/authorize?")
    assert "client_id=cid" in url and "state=st8" in url and "response_type=code" in url
    assert "offline_access" in url


def test_exchange_code_persists_tokens(tmp_path: Path, httpx_mock):
    httpx_mock.add_response(
        url=TOKEN_URL,
        json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
    )
    store = TokenStore(tmp_path / "tok.json")
    exchange_code(store, TOKEN_URL, "cid", "sec", "the-code", "http://localhost:8765/callback")
    assert store.access_token() == "at"


def test_access_token_refreshes_when_expired(tmp_path: Path, httpx_mock):
    store = TokenStore(tmp_path / "tok.json")
    store.save({"access_token": "old", "refresh_token": "rt", "expires_at": time.time() - 10})
    httpx_mock.add_response(
        url=TOKEN_URL,
        json={"access_token": "new", "refresh_token": "rt2", "expires_in": 3600},
    )
    tok = refresh(store, TOKEN_URL, "cid", "sec")
    assert tok == "new"
    assert json.loads((tmp_path / "tok.json").read_text())["refresh_token"] == "rt2"
