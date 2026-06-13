from pathlib import Path

import pytest

from lucid_vault_exporter.config import Config, ConfigError, Settings


def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("LUCID_CLIENT_ID", "cid")
    monkeypatch.setenv("LUCID_CLIENT_SECRET", "sec")
    s = Settings(_env_file=None)
    assert s.lucid_client_id == "cid"
    assert s.lucid_api_base == "https://api.lucid.co"


def test_config_loads_yaml(tmp_path: Path):
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("output_dir: ./out\nproducts: [lucidchart]\n", encoding="utf-8")
    cfg = Config.load(cfg_file)
    assert cfg.products == ["lucidchart"]
    assert cfg.browser.formats == ["pdf", "vsdx"]  # defaults survive partial files


def test_config_rejects_unknown_product(tmp_path: Path):
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("output_dir: ./out\nproducts: [visio]\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        Config.load(cfg_file)


def test_config_missing_file_raises(tmp_path: Path):
    with pytest.raises(ConfigError):
        Config.load(tmp_path / "nope.yml")
