from fastapi.testclient import TestClient

from lucid_vault_exporter import web
from lucid_vault_exporter.web import create_app


def _client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yml").write_text("output_dir: ./vault\n", encoding="utf-8")
    web._registry.jobs.clear()
    web._registry.active = None
    web._conn.update({"status": "idle", "user": None, "error": None})
    web._browser.update({"status": "idle", "error": None})
    return TestClient(create_app())


def test_index_serves_html(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.get("/")
    assert r.status_code == 200 and "Lucid Vault Exporter" in r.text


def test_status_shape(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    from lucid_vault_exporter.state import StateDB
    with StateDB.open(tmp_path / "vault") as db:
        db.upsert_document(document_id="d1", title="T", product="lucidchart", folder_path="")
        db.set_artifact("d1", "png", "ok")
    data = c.get("/api/status").json()
    assert data["documents"] == 1
    assert data["artifacts"]["png"]["ok"] == 1
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
