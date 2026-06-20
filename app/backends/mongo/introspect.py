"""Schema inference for Mongo — there's no catalog to introspect, so this
samples documents per collection and infers field name -> type -> presence%.

Deliberately mirrors db/introspect.py's SchemaSnapshot/TableInfo shape
(same attribute/method names: .dialect, .to_prompt(), .table_names,
.to_api()) so app/agent/orchestrator.py works against this unchanged via
duck typing — it never imports SQL or Mongo introspection types directly,
only calls schema.get() and reads those names off whatever it gets back.

Sampling is `find().limit(n)`, not `$sample` — slightly less statistically
random, but works identically against every Mongo version and against
mongomock in tests, which is worth more than true randomness here.
"""
from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass, field

from app.backends.mongo.engine import ReadOnlyDatabase
from app.config import AppConfig

_CACHE_TTL_SECONDS = 300


@dataclass
class FieldInfo:
    name: str
    likely_type: str
    presence_pct: int       # % of sampled docs that had this field at all
    example: str = ""


@dataclass
class CollectionInfo:
    name: str
    fields: list[FieldInfo] = field(default_factory=list)
    row_count: int | None = None     # named row_count, not doc_count, to match
    sample_rows: list[dict] = field(default_factory=list)  # TableInfo's shape


@dataclass
class MongoSchemaSnapshot:
    dialect: str = "mongodb"
    tables: list[CollectionInfo] = field(default_factory=list)  # named `tables` to
                                                                   # match SchemaSnapshot
    captured_at: float = 0.0   # used for the 5-min cache + the /api/schema payload

    @property
    def table_names(self) -> set[str]:
        return {t.name.lower() for t in self.tables}

    def to_prompt(self) -> str:
        out: list[str] = [
            "Database: MongoDB (schemaless — field list below is inferred "
            "from a document sample, not guaranteed exhaustive)",
            "",
        ]
        for c in self.tables:
            rc = f"  -- ~{c.row_count} documents" if c.row_count is not None else ""
            out.append(f"COLLECTION {c.name}{rc}")
            for f in c.fields:
                out.append(f"  {f.name}: {f.likely_type} (present in ~{f.presence_pct}% of sampled docs)")
            if c.sample_rows:
                out.append(f"  sample: {c.sample_rows}")
        return "\n".join(out)

    def to_api(self) -> dict:
        return {
            "dialect": self.dialect,
            "captured_at": self.captured_at,
            "tables": [
                {
                    "name": c.name,
                    "row_count": c.row_count,
                    "columns": [
                        {"name": f.name, "type": f.likely_type, "nullable": True, "pk": False}
                        for f in c.fields
                    ],
                    "foreign_keys": [],   # Mongo has no native FK concept
                }
                for c in self.tables
            ],
        }


class MongoSchemaService:
    def __init__(self, db: ReadOnlyDatabase, cfg: AppConfig):
        self._db = db
        self._cfg = cfg
        self._lock = threading.Lock()
        self._snapshot: MongoSchemaSnapshot | None = None

    def get(self, force: bool = False) -> MongoSchemaSnapshot:
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

    def _introspect(self) -> MongoSchemaSnapshot:
        snap = MongoSchemaSnapshot(captured_at=time.time())
        sample_n = max(self._cfg.guardrails.sample_rows_in_schema, 10)

        visible = [n for n in sorted(self._db.list_collection_names()) if self._visible(n)]

        for name in visible:
            coll = self._db.get_collection(name)
            try:
                count = coll.estimated_document_count()
            except Exception:
                count = None

            docs = list(coll.find().limit(sample_n))
            fields = _infer_fields(docs)

            shown = self._cfg.guardrails.sample_rows_in_schema
            sample_rows = [
                {k: _short(v) for k, v in d.items() if k != "_id"}
                for d in docs[:shown]
            ] if shown > 0 else []

            snap.tables.append(CollectionInfo(
                name=name, fields=fields, row_count=count, sample_rows=sample_rows,
            ))
        return snap


def _infer_fields(docs: list[dict]) -> list[FieldInfo]:
    if not docs:
        return []
    n = len(docs)
    presence: Counter = Counter()
    types: dict[str, Counter] = {}
    examples: dict[str, str] = {}

    for d in docs:
        for k, v in d.items():
            if k == "_id":
                continue
            presence[k] += 1
            t = _type_name(v)
            types.setdefault(k, Counter())[t] += 1
            if k not in examples:
                examples[k] = _short(v)

    out = []
    for k in presence:
        likely = types[k].most_common(1)[0][0]
        out.append(FieldInfo(
            name=k, likely_type=likely,
            presence_pct=round(presence[k] / n * 100),
            example=examples[k],
        ))
    return sorted(out, key=lambda f: -f.presence_pct)


def _type_name(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def _short(v, n: int = 40) -> str:
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"
