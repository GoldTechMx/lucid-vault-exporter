from pathlib import Path

from lucid_vault_exporter import web


def test_browse_lists_subdirs(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "f.txt").write_text("x")
    out = web._browse(str(tmp_path))
    names = [d["name"] for d in out["dirs"]]
    assert names == ["a", "b"]  # sorted, dirs only
    assert out["path"] == str(tmp_path.resolve())
    assert out["writable"] is True


def test_browse_bad_path_falls_back_home():
    out = web._browse("Z:/no/such/dir/at/all/12345")
    assert Path(out["path"]).exists()  # fell back to an existing dir, never crashes


def test_check_path_reports_will_be_created(tmp_path):
    out = web._check_path(str(tmp_path / "new_vault"))
    assert out["ok"] is True and out["exists"] is False and "created" in out["msg"]


def test_check_path_empty():
    out = web._check_path("   ")
    assert out["ok"] is False


def test_write_env_replaces_only_lucid_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text("OTHER=keep\nLUCID_CLIENT_ID=old\n", encoding="utf-8")
    web._write_env("cid", "sec")
    text = Path(".env").read_text(encoding="utf-8")
    assert "OTHER=keep" in text
    assert "LUCID_CLIENT_ID=cid" in text and "LUCID_CLIENT_SECRET=sec" in text
    assert "old" not in text
