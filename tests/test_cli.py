import sys
from pathlib import Path

from typer.testing import CliRunner

from lucid_vault_exporter.cli import app

runner = CliRunner()


def test_init_writes_config(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / "config.yml").is_file()


def test_init_refuses_overwrite(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yml").write_text("output_dir: ./x\n")
    result = runner.invoke(app, ["init"])
    assert result.exit_code != 0


def test_verify_reports_counts(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yml").write_text("output_dir: ./vault\n")
    from lucid_vault_exporter.state import StateDB
    with StateDB.open(tmp_path / "vault") as db:
        db.upsert_document(document_id="d1", title="T", product="lucidchart", folder_path="")
        db.set_artifact("d1", "png", "ok")
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0
    assert "png" in result.output


def test_export_only_browser_with_browser_disabled_writes_manifest(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yml").write_text(
        "output_dir: ./vault\nbrowser:\n  enabled: false\n", encoding="utf-8"
    )
    from lucid_vault_exporter.state import StateDB
    with StateDB.open(tmp_path / "vault") as db:
        db.upsert_document(document_id="d1", title="T", product="lucidchart", folder_path="")
    result = runner.invoke(app, ["export", "--only-browser"])
    assert result.exit_code == 0, result.output
    # neither phase ran; manifest was written
    assert (tmp_path / "vault" / "_manifest" / "inventory.csv").is_file()
    assert "Export complete" in result.output


def test_serve_without_uvicorn_exits_with_hint(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "uvicorn" or name.startswith("uvicorn"):
            raise ImportError("no uvicorn")
        return real_import(name, *args, **kwargs)

    # Remove any cached uvicorn module so the import inside serve() is attempted fresh
    monkeypatch.setitem(sys.modules, "uvicorn", None)  # type: ignore[arg-type]
    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 1
    assert "web" in result.output.lower()
