"""Database engine with defence-in-depth read-only hardening.

Layer 1 is the SQL firewall (guardrails/validator.py). This module is
Layer 2: even if a write somehow reached the driver, the session itself
refuses it, and the server kills long-running statements where the
backend supports it.
"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from app.config import AppConfig


def build_engine(cfg: AppConfig) -> Engine:
    url = cfg.database.url
    timeout_s = cfg.guardrails.statement_timeout_seconds

    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )
    backend = engine.url.get_backend_name()

    if backend == "sqlite":
        @event.listens_for(engine, "connect")
        def _sqlite_ro(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA query_only = ON")   # hard read-only switch
            cur.close()

    elif backend in ("postgresql", "postgres"):
        @event.listens_for(engine, "connect")
        def _pg_ro(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("SET default_transaction_read_only = on")
            cur.execute(f"SET statement_timeout = {int(timeout_s * 1000)}")
            cur.close()

    elif backend in ("mysql", "mariadb"):
        @event.listens_for(engine, "connect")
        def _mysql_ro(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            try:
                cur.execute(f"SET SESSION max_execution_time = {int(timeout_s * 1000)}")
            except Exception:
                pass  # MariaDB < 10.1 / permission-limited users
            cur.close()

    return engine
