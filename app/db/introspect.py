"""Schema snapshot: what the LLM and the UI are allowed to know about the DB.

Introspected once via SQLAlchemy, cached for 5 minutes, filtered through
the table allow/deny lists, and rendered two ways: JSON for the sidebar,
compact DDL-ish text for the model prompt.

B2 fix: reuse a single connection for all per-table queries instead of
opening a new connection per table.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.config import AppConfig

_CACHE_TTL_SECONDS = 300


@dataclass
class TableInfo:
    name: str
    columns: list[dict] = field(default_factory=list)
    foreign_keys: list[str] = field(default_factory=list)
    row_count: int | None = None
    sample_rows: list[dict] = field(default_factory=list)


@dataclass
class SchemaSnapshot:
    dialect: str
    tables: list[TableInfo] = field(default_factory=list)
    captured_at: float = 0.0

    @property
    def table_names(self) -> set[str]:
        return {t.name.lower() for t in self.tables}

    def to_prompt(self) -> str:
        out: list[str] = [f"Database dialect: {self.dialect}", ""]
        for t in self.tables:
            cols = ", ".join(
                f"{c['name']} {c['type']}{' PK' if c['pk'] else ''}"
                for c in t.columns
            )
            rc = f"  -- ~{t.row_count} rows" if t.row_count is not None else ""
            out.append(f"TABLE {t.name} ({cols}){rc}")
            for fk in t.foreign_keys:
                out.append(f"  FK: {fk}")
            if t.sample_rows:
                out.append(f"  sample: {t.sample_rows}")
        return "\n".join(out)

    def to_api(self) -> dict:
        return {
            "dialect": self.dialect,
            "captured_at": self.captured_at,
            "tables": [
                {
                    "name": t.name,
                    "row_count": t.row_count,
                    "columns": t.columns,
                    "foreign_keys": t.foreign_keys,
                }
                for t in self.tables
            ],
        }


class SchemaService:
    def __init__(self, engine: Engine, cfg: AppConfig):
        self._engine = engine
        self._cfg = cfg
        self._lock = threading.Lock()
        self._snapshot: SchemaSnapshot | None = None

    def get(self, force: bool = False) -> SchemaSnapshot:
        with self._lock:
            fresh = (
                self._snapshot is not None
                and time.time() - self._snapshot.captured_at < _CACHE_TTL_SECONDS
            )
            if fresh and not force:
                return self._snapshot
            self._snapshot = self._introspect()
            return self._snapshot

    def _visible(self, name: str) -> bool:
        g = self._cfg.guardrails
        low = name.lower()
        if low in {d.lower() for d in g.denied_tables}:
            return False
        if g.allowed_tables:
            return low in {a.lower() for a in g.allowed_tables}
        return True

    def _introspect(self) -> SchemaSnapshot:
        insp = inspect(self._engine)
        dialect = self._engine.url.get_backend_name()
        snap = SchemaSnapshot(dialect=dialect, captured_at=time.time())
        sample_n = self._cfg.guardrails.sample_rows_in_schema

        visible_tables = [n for n in sorted(insp.get_table_names()) if self._visible(n)]

        # B2 FIX: one connection for all per-table queries, not one per table
        with self._engine.connect() as conn:
            for name in visible_tables:
                pks = set(insp.get_pk_constraint(name).get("constrained_columns") or [])
                ti = TableInfo(
                    name=name,
                    columns=[
                        {
                            "name": c["name"],
                            "type": str(c["type"]),
                            "nullable": bool(c.get("nullable", True)),
                            "pk": c["name"] in pks,
                        }
                        for c in insp.get_columns(name)
                    ],
                    foreign_keys=[
                        f"{name}.{','.join(fk['constrained_columns'])} -> "
                        f"{fk['referred_table']}.{','.join(fk['referred_columns'])}"
                        for fk in insp.get_foreign_keys(name)
                    ],
                )
                q = self._engine.dialect.identifier_preparer.quote(name)
                try:
                    ti.row_count = conn.execute(
                        text(f"SELECT COUNT(*) FROM {q}")  # noqa: S608
                    ).scalar()
                except Exception:
                    ti.row_count = None

                if sample_n > 0:
                    try:
                        rows = conn.execute(
                            text(f"SELECT * FROM {q} LIMIT {int(sample_n)}")  # noqa: S608
                        )
                        cols = list(rows.keys())
                        ti.sample_rows = [
                            {c: _short(v) for c, v in zip(cols, r)} for r in rows
                        ]
                    except Exception:
                        ti.sample_rows = []

                snap.tables.append(ti)
        return snap


def _short(v, n: int = 40):
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"
