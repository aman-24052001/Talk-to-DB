"""Guarded SQL execution with wall-clock timeout, row cap, cell truncation,
and an audit trail of everything executed or blocked.

B1 fix: ThreadPoolExecutor.shutdown() is called from the FastAPI lifespan
so threads are cleanly joined on server stop.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import AppConfig

_MAX_CELL_CHARS = 400
log = logging.getLogger("talk_to_db")


@dataclass
class QueryResult:
    ok: bool
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: int = 0
    error: str | None = None


class QueryExecutor:
    def __init__(self, engine: Engine, cfg: AppConfig):
        self._engine = engine
        self._cfg = cfg
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="sql"
        )
        self._audit_path = Path(cfg.logging.audit_file)
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)

    def shutdown(self) -> None:
        """B1 FIX: cleanly drain thread pool on server shutdown."""
        self._pool.shutdown(wait=True, cancel_futures=False)

    def run(self, sql: str) -> QueryResult:
        start = time.perf_counter()
        timeout = self._cfg.guardrails.statement_timeout_seconds
        future = self._pool.submit(self._run_blocking, sql)
        try:
            result = future.result(timeout=timeout + 1)
        except concurrent.futures.TimeoutError:
            result = QueryResult(
                ok=False, sql=sql,
                error=f"Query exceeded the {timeout}s timeout and was abandoned.",
            )
        except Exception as e:
            result = QueryResult(ok=False, sql=sql, error=_clean_db_error(e))
        result.elapsed_ms = int((time.perf_counter() - start) * 1000)
        self.audit("executed" if result.ok else "failed", sql,
                   rows=result.row_count, error=result.error)
        return result

    def _run_blocking(self, sql: str) -> QueryResult:
        max_rows = self._cfg.guardrails.max_rows
        with self._engine.connect() as conn:
            cursor = conn.execute(text(sql))
            columns = list(cursor.keys())
            fetched = cursor.fetchmany(max_rows + 1)
            truncated = len(fetched) > max_rows
            rows = [
                [_cell(v) for v in row]
                for row in fetched[:max_rows]
            ]
            return QueryResult(
                ok=True, sql=sql, columns=columns, rows=rows,
                row_count=len(rows), truncated=truncated,
            )

    def audit(self, event: str, sql: str, **extra) -> None:
        record = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "sql": sql,
            **{k: v for k, v in extra.items() if v is not None},
        }
        try:
            with self._audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            log.warning("audit log write failed", exc_info=True)


def _cell(v):
    if v is None:
        return None
    if isinstance(v, (int, float, bool)):
        return v
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(v)} bytes>"
    s = str(v)
    return s if len(s) <= _MAX_CELL_CHARS else s[:_MAX_CELL_CHARS] + "…"


def _clean_db_error(e: Exception) -> str:
    import re
    msg = str(getattr(e, "orig", e)).splitlines()[0]
    msg = re.sub(r"://([^:/@\s]+):([^@\s]+)@", r"://\1:***@", msg)
    return msg[:500]
