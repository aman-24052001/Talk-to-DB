"""Typed configuration. Loads config.yaml once; ANTHROPIC_API_KEY env var wins.

I2 fix: single version string lives here.
B4 fix: history_turns is configurable.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

__version__ = "3.0.0"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("TALK_TO_DB_CONFIG", PROJECT_ROOT / "config.yaml"))


class AnthropicCfg(BaseModel):
    api_key: str = ""
    model: str = "claude-sonnet-4-6"


class DatabaseCfg(BaseModel):
    url: str = "sqlite:///data/demo.db"


class GuardrailsCfg(BaseModel):
    read_only: bool = True
    max_rows: int = Field(200, ge=1, le=10_000)
    max_result_rows_to_model: int = Field(50, ge=1, le=500)
    statement_timeout_seconds: int = Field(15, ge=1, le=300)
    max_agent_turns: int = Field(6, ge=1, le=12)
    # B4 FIX: how many prior conversation turns to keep in model context
    history_turns: int = Field(6, ge=1, le=20)
    allowed_tables: list[str] = []
    denied_tables: list[str] = []
    sample_rows_in_schema: int = Field(3, ge=0, le=10)

    @field_validator("read_only")
    @classmethod
    def _refuse_write_mode(cls, v: bool) -> bool:
        if not v:
            raise ValueError(
                "read_only=false is not supported. Talk-to-DB is an analytics "
                "tool; write access for an LLM agent is a deliberate non-goal."
            )
        return v


class ServerCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7860
    auth_token: str = ""
    rate_limit_per_minute: int = Field(30, ge=1, le=1000)


class LoggingCfg(BaseModel):
    audit_file: str = "data/audit.log"


class AppConfig(BaseModel):
    anthropic: AnthropicCfg = AnthropicCfg()
    database: DatabaseCfg = DatabaseCfg()
    guardrails: GuardrailsCfg = GuardrailsCfg()
    server: ServerCfg = ServerCfg()
    logging: LoggingCfg = LoggingCfg()

    @property
    def resolved_api_key(self) -> str:
        return os.environ.get("ANTHROPIC_API_KEY") or self.anthropic.api_key


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    raw: dict = {}
    if CONFIG_PATH.exists():
        raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    cfg = AppConfig(**raw)
    url = cfg.database.url
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        rel = url.removeprefix("sqlite:///")
        cfg.database.url = f"sqlite:///{(PROJECT_ROOT / rel).resolve()}"
    return cfg
