"""Backend factory.

Picks and constructs a backend (engine + schema service + executor +
adapter) from config.database.resolved_type. Today only "sql" exists.
This is the single place a new backend (e.g. "mongodb") gets registered —
once that adapter exists, main.py and the agent do not change at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.backends.base import BackendAdapter
from app.backends.sql import SQLAdapter
from app.config import AppConfig
from app.db.engine import build_engine
from app.db.executor import QueryExecutor
from app.db.introspect import SchemaService


@dataclass
class Backend:
    """Everything main.py needs to wire up one connected datastore."""
    engine: Any           # SQLAlchemy Engine today; backend-specific in general
    schema: SchemaService
    executor: QueryExecutor
    adapter: BackendAdapter


def build_backend(cfg: AppConfig) -> Backend:
    backend_type = cfg.resolved_database_type

    if backend_type == "sql":
        engine = build_engine(cfg)
        schema = SchemaService(engine, cfg)
        executor = QueryExecutor(engine, cfg)
        adapter = SQLAdapter(executor)
        return Backend(engine=engine, schema=schema, executor=executor, adapter=adapter)

    raise ValueError(
        f"Unsupported database backend '{backend_type}'. "
        "Only SQL backends (sqlite/postgres/mysql, via database.url) are "
        "implemented today. Set database.type explicitly only if you need "
        "to override URL-scheme inference."
    )
