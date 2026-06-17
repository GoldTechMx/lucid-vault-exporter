from fastapi.testclient import TestClient

from lucid_vault_exporter import web
from lucid_vault_exporter.web import create_app


def _client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yml").write_text("output_dir: ./vault\n", encoding="utf-8")
    web._registry.jobs.clear()
    web._registry.active = None
    web._conn.update({"status": "idle", "user": None, "error": None,
                      "client_id": "", "client_secret": ""})
    web._browser.update({"status": "idle", "error": None})
    return TestClient(create_app())


def test_index_serves_html(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.get("/")
    assert r.status_code == 200 and "Lucid Vault Exporter" in r.text


def test_status_auto_connects_with_saved_creds(tmp_path, monkeypatch):
    import json
    import time

    import httpx

    c = _client(tmp_path, monkeypatch)
    # saved creds + a stored token that is not near expiry
    (tmp_path / ".lucid_tokens.json").write_text(
        json.dumps({"access_token": "at", "refresh_token": "rt",
                    "expires_at": time.time() + 3600}),
        encoding="utf-8",
    )
    web._conn.update({"client_id": "cid", "client_secret": "sec", "status": "idle"})

    class FakeResp:
        def __init__(self, code, payload=None):
            self.status_code = code
            self._p = payload or {}

        def json(self):
            return self._p

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResp(200))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResp(200, {"name": "Boby"}))

    data = c.get("/api/status").json()
    assert data["connected"] is True
    assert data["user"] == "Boby"
    assert data["saved_client_id"] == "cid"


def test_status_stays_idle_without_token(tmp_path, monkeypatch):
    # creds present but no .lucid_tokens.json -> must NOT auto-connect (and must not call network)
    c = _client(tmp_path, monkeypatch)
    web._conn.update({"client_id": "cid", "client_secret": "sec", "status": "idle"})
    data = c.get("/api/status").json()
    assert data["connected"] is False


def test_status_shape(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    from lucid_vault_exporter.state import StateDB
    with StateDB.open(tmp_path / "vault") as db:
        db.upsert_document(document_id="d1", title="T", product="lucidchart", folder_path="")
        db.set_artifact("d1", "png", "ok")
    data = c.get("/api/status").json()
    assert data["documents"] == 1
    assert data["artifacts"]["png"]["ok"] == 1
    assert data["artifacts"]["pdf"]["ok"] == 0
    assert data["artifacts"]["vsdx"]["ok"] == 0
    assert "connected" in data and "browser_logged_in" in data


def test_browse_and_check_path(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    assert c.get("/api/browse", params={"path": str(tmp_path)}).status_code == 200
    chk = c.get("/api/check-path", params={"path": str(tmp_path / "v")}).json()
    assert chk["ok"] is True


def test_connect_returns_authorize_url(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "_start_oauth_capture", lambda state: None)
    r = c.post("/api/connect", json={"client_id": "cid", "client_secret": "sec",
                                     "remember": False})
    body = r.json()
    assert r.status_code == 200
    assert body["authorize_url"].startswith("https://lucid.app/oauth2/authorize?")
    assert "client_id=cid" in body["authorize_url"]


def test_run_rejects_browser_command_without_login(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    web._conn["status"] = "connected"
    r = c.post("/api/run", json={"command": "export_browser", "options": {}})
    assert r.status_code == 400
    assert "sign in" in r.json()["detail"].lower()


def test_run_and_job_lifecycle_with_fake_client(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    web._conn["status"] = "connected"

    class FakeClient:
        def search_documents(self, **kw):
            yield {"documentId": "d1", "title": "Uno", "product": "lucidchart",
                   "parent": None, "pageCount": 1}
        def get_folder(self, fid):
            return None
        def export_page_png(self, doc_id, *, page):
            from lucid_vault_exporter.lucid_client import PageNotFound
            if page > 1:
                raise PageNotFound("x")
            return b"\x89PNG" + doc_id.encode()
        def close(self):
            pass

    monkeypatch.setattr(web, "_make_client", lambda cfg: FakeClient())
    r = c.post("/api/run", json={"command": "export_api", "options": {"limit": 1}})
    job_id = r.json()["job_id"]
    import time
    for _ in range(50):
        st = c.get(f"/api/jobs/{job_id}").json()
        if st["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.05)
    assert st["status"] == "done", st
    assert st["result"]["png_ok"] == 1
    assert st["log_count"] >= len(st["logs"])


def test_job_control_unknown_action(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    job = web._registry.create("export_api")
    r = c.post(f"/api/jobs/{job.id}/frobnicate")
    assert r.status_code == 400


def test_index_has_cards_and_tips(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    html = c.get("/").text
    for needle in ["How this works", "Connect to Lucid", "Sign in for PDF/VSDX",
                   "Output folder", "What to export", "Event console",
                   "http://localhost:8765/callback"]:
        assert needle in html, needle
