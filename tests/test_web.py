from fastapi.testclient import TestClient

from lucid_vault_exporter.web import create_app


def test_index_serves_html(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yml").write_text("output_dir: ./vault\n")
    client = TestClient(create_app())
    resp = client.get("/")
    assert resp.status_code == 200 and "Lucid Vault Exporter" in resp.text


def test_status_endpoint_counts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yml").write_text("output_dir: ./vault\n")
    from lucid_vault_exporter.state import StateDB
    with StateDB.open(tmp_path / "vault") as db:
        db.upsert_document(document_id="d1", title="T", product="lucidchart", folder_path="")
        db.set_artifact("d1", "png", "ok")
    client = TestClient(create_app())
    data = client.get("/api/status").json()
    assert data["documents"] == 1 and data["artifacts"]["png"]["ok"] == 1


def test_stop_returns_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yml").write_text("output_dir: ./vault\n")
    client = TestClient(create_app())
    resp = client.post("/api/stop")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_status_no_config_returns_400(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no config.yml written
    client = TestClient(create_app())
    resp = client.get("/api/status")
    assert resp.status_code == 400
    assert "error" in resp.json()
