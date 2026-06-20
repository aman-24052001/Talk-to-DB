"""Guarded Mongo execution. Mirrors db/executor.py's shape: wall-clock
timeout, row cap, cell truncation, JSON-lines audit trail — flattened
into the same generic columns/rows shape the existing UI and AskResponse
contract already render, so neither needed to change for this backend.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.backends.mongo.engine import ReadOnlyDatabase
from app.backends.mongo.validator import ValidatedMongoQuery
from app.config import AppConfig

_MAX_CELL_CHARS = 400
log = logging.getLogger("talk_to_db")


@dataclass
class QueryResult:
    """Same shape as db/executor.py's QueryResult — duck-type compatible
    with everything in orchestrator.py that reads qr.ok/.columns/.rows/etc."""
    ok: bool
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: int = 0
    error: str | None = None


class MongoExecutor:
    def __init__(self, db: ReadOnlyDatabase, cfg: AppConfig):
        self._db = db
        self._cfg = cfg
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="mongo"
        )
        self._audit_path = Path(cfg.logging.audit_file)
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)

    def run(self, validated: ValidatedMongoQuery) -> QueryResult:
        start = time.perf_counter()
        timeout = self._cfg.guardrails.statement_timeout_seconds
        future = self._pool.submit(self._run_blocking, validated)
        try:
            result = future.result(timeout=timeout + 1)
        except concurrent.futures.TimeoutError:
            result = QueryResult(
                ok=False, sql=validated.sql,
                error=f"Query exceeded the {timeout}s timeout and was abandoned.",
            )
        except Exception as e:
            result = QueryResult(ok=False, sql=validated.sql, error=_clean_mongo_error(e))
        result.elapsed_ms = int((time.perf_counter() - start) * 1000)
        self.audit("executed" if result.ok else "failed", validated.sql,
                   rows=result.row_count, error=result.error)
        return result

    def _run_blocking(self, validated: ValidatedMongoQuery) -> QueryResult:
        timeout_ms = int(self._cfg.guardrails.statement_timeout_seconds * 1000)
        coll = self._db.get_collection(validated.collection)

        if validated.operation == "find":
            f = validated.spec
            cursor = coll.find(
                f.get("filter") or {}, f.get("projection"),
                max_time_ms=timeout_ms,
            )
            if f.get("sort"):
                cursor = cursor.sort(list(f["sort"].items()))
            docs = list(cursor.limit(f.get("limit") or self._cfg.guardrails.max_rows))
        else:
            docs = list(coll.aggregate(validated.spec, maxTimeMS=timeout_ms))

        max_rows = self._cfg.guardrails.max_rows
        truncated = len(docs) > max_rows
        docs = docs[:max_rows]

        columns = _columns_for(docs)
        rows = [[_cell(d.get(c)) for c in columns] for d in docs]

        return QueryResult(
            ok=True, sql=validated.sql, columns=columns, rows=rows,
            row_count=len(rows), truncated=truncated,
        )

    def audit(self, event: str, query: str, **extra: Any) -> None:
        record = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "sql": query,
            **{k: v for k, v in extra.items() if v is not None},
        }
        try:
            with self._audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            log.warning("audit log write failed", exc_info=True)


def _columns_for(docs: list[dict]) -> list[str]:
    """Union of keys across the returned docs, _id first, stable order."""
    cols: list[str] = []
    seen: set[str] = set()
    for d in docs:
        for k in d.keys():
            if k not in seen:
                seen.add(k)
                cols.append(k)
    if "_id" in cols:
        cols.remove("_id")
        cols.insert(0, "_id")
    return cols


def _cell(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (int, float, bool)):
        return v
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    if isinstance(v, (dict, list)):
        s = json.dumps(v, default=str, ensure_ascii=False)
        return s if len(s) <= _MAX_CELL_CHARS else s[:_MAX_CELL_CHARS] + "…"
    s = str(v)
    return s if len(s) <= _MAX_CELL_CHARS else s[:_MAX_CELL_CHARS] + "…"


def _clean_mongo_error(e: Exception) -> str:
    import re
    msg = str(e).splitlines()[0]
    msg = re.sub(r"://([^:/@\s]+):([^@\s]+)@", r"://\1:***@", msg)
    return msg[:500]
