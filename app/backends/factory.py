"""Backend factory.

Picks and constructs a backend (engine + schema service + executor +
adapter) from config.database.resolved_type. Today only "sql" exists.
This is the single place a new backend (e.g. "mongodb") gets registered —
once that adapter exists, main.py and the agent do not change at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.backends.base import BackendAdapter
from app.backends.sql import SQLAdapter
from app.config import AppConfig, DatabaseCfg, infer_backend_type
from app.db.engine import build_engine
from app.db.executor import QueryExecutor
from app.db.introspect import SchemaService


@dataclass
class Backend:
    """Everything main.py needs to wire up one connected datastore."""
    engine: Any           # SQLAlchemy Engine for "sql"; backend-specific client otherwise
    schema: SchemaService
    executor: QueryExecutor
    adapter: BackendAdapter
    close: Callable[[], None]   # generic shutdown — never assume .dispose() exists


def build_backend(cfg: AppConfig, database: DatabaseCfg | None = None) -> Backend:
    """Build one backend. By default (no `database` arg) this is cfg.database
    — the original single-source behavior, completely unchanged. Passing an
    explicit `database` (used by app/hub.py for named multi-source entries)
    connects to that instead, while every other setting (guardrails,
    anthropic, logging) stays shared from `cfg`."""
    db_cfg = database or cfg.database
    effective_cfg = cfg if database is None else cfg.model_copy(update={"database": db_cfg})
    backend_type = db_cfg.type or infer_backend_type(db_cfg.url)

    if backend_type == "sql":
        engine = build_engine(effective_cfg)
        schema = SchemaService(engine, effective_cfg)
        executor = QueryExecutor(engine, effective_cfg)
        adapter = SQLAdapter(executor)
        return Backend(
            engine=engine, schema=schema, executor=executor, adapter=adapter,
            close=engine.dispose,
        )

    if backend_type == "mongodb":
        from app.backends.mongo.factory import build_mongo_backend
        return build_mongo_backend(effective_cfg)

    raise ValueError(
        f"Unsupported database backend '{backend_type}'. "
        "Implemented: 'sql' (sqlite/postgres/mysql via database.url), "
        "'mongodb'. Set database.type explicitly only if you need to "
        "override URL-scheme inference."
    )
