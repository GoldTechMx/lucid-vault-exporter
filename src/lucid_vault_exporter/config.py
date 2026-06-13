"""Two config sources: .env (secrets, via pydantic-settings) and config.yml (run options)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

import yaml
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

VALID_PRODUCTS = ("lucidchart", "lucidspark", "lucidscale")


class ConfigError(Exception):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    lucid_client_id: str = ""
    lucid_client_secret: str = ""
    lucid_api_base: str = "https://api.lucid.co"
    lucid_auth_base: str = "https://lucid.app"


class BrowserConfig(BaseModel):
    enabled: bool = True
    formats: list[Literal["pdf", "vsdx"]] = ["pdf", "vsdx"]
    headless: bool = True
    min_delay_seconds: float = 3.0
    max_delay_seconds: float = 7.0


class RateLimitConfig(BaseModel):
    export_per_5s: int = 60
    search_per_5s: int = 240


class Config(BaseModel):
    output_dir: Path
    products: list[Literal["lucidchart", "lucidspark", "lucidscale"]] = Field(
        default_factory=lambda: cast(
            list[Literal["lucidchart", "lucidspark", "lucidscale"]], list(VALID_PRODUCTS)
        )
    )
    exclude_trashed: bool = True
    png_dpi: int = 160
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)

    @classmethod
    def load(cls, path: Path) -> Config:
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}. Run `lucid-vault-exporter init`.")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return cls.model_validate(raw)
        except (yaml.YAMLError, ValidationError) as exc:
            raise ConfigError(f"Invalid config {path}: {exc}") from exc
