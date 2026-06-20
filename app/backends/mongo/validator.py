"""Mongo firewall.

There is no sqlglot-equivalent for Mongo — a query here is a Python dict
(a filter) or a list of dicts (a pipeline), not a parseable grammar with
an AST library behind it. So this validates structure directly: every
operation must be `find` or `aggregate`, every pipeline stage's key must
be in an explicit allowlist, and any stage/operator that can execute
arbitrary JS or write data is hard-blocked regardless of nesting.

Policy, mirroring guardrails/validator.py's SQL policy:
  - exactly one operation per tool call (find OR aggregate)
  - operation must target an introspected+allowlisted collection
  - aggregate pipelines may only use allowlisted stages
  - $where, $function, $accumulator (arbitrary JS) are never allowed,
    anywhere in the structure, not just as a top-level stage
  - $out / $merge (write stages) are never allowed
  - a row LIMIT is force-injected/clamped, same as SQL
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Stages that only ever read/reshape data.
ALLOWED_STAGES = {
    "$match", "$project", "$group", "$sort", "$limit", "$skip", "$unwind",
    "$count", "$lookup", "$addFields", "$set", "$unset", "$facet",
    "$sortByCount", "$bucket", "$bucketAuto", "$replaceRoot", "$replaceWith",
    "$sample", "$redact",
}

# Stages/operators that write data or execute arbitrary code. Checked
# recursively through the whole structure, not just as top-level stage
# names — a $function tucked inside a $project's expression is just as
# dangerous as one used as its own stage.
FORBIDDEN_KEYS = {
    "$out", "$merge", "$function", "$accumulator", "$where",
    "$planCacheClear", "$currentOp", "$collStats", "$indexStats",
}


class MongoRejected(Exception):
    """Raised when a query fails policy. Message is fed back to the model
    so it can self-correct, same role as SQLRejected."""


@dataclass
class ValidatedMongoQuery:
    sql: str                        # canonical JSON text — same field name as
                                     # ValidatedSQL.sql, see base.py's note on
                                     # deferring the sql->query rename
    operation: str = ""             # "find" | "aggregate"
    collection: str = ""
    spec: dict | list = field(default_factory=dict)   # filter dict, or pipeline list
    tables: list[str] = field(default_factory=list)    # [collection], for parity with SQL


def validate_mongo_query(
    raw_query: str,
    *,
    dialect: str,           # unused, kept for BackendAdapter signature parity
    known_tables: set[str],  # known collection names (lowercased)
    max_rows: int,
) -> ValidatedMongoQuery:
    if not raw_query or not raw_query.strip():
        raise MongoRejected("Empty query.")

    try:
        spec = json.loads(raw_query)
    except json.JSONDecodeError as e:
        raise MongoRejected(f"Query is not valid JSON: {e}") from e

    if not isinstance(spec, dict):
        raise MongoRejected("Query must be a JSON object with operation/collection/query.")

    operation = spec.get("operation")
    collection = spec.get("collection")
    query = spec.get("query")

    if operation not in ("find", "aggregate"):
        raise MongoRejected(
            f"operation must be 'find' or 'aggregate', got {operation!r}."
        )
    if not isinstance(collection, str) or not collection:
        raise MongoRejected("collection must be a non-empty string.")
    if known_tables and collection.lower() not in known_tables:
        raise MongoRejected(
            f"Collection '{collection}' is not in the allowed schema. "
            f"Available collections: {', '.join(sorted(known_tables))}."
        )

    if operation == "find":
        validated_spec = _validate_find(query, spec, max_rows)
    else:
        validated_spec = _validate_aggregate(query, collection, known_tables, max_rows)

    canonical = {
        "operation": operation, "collection": collection, "query": validated_spec,
    }
    return ValidatedMongoQuery(
        sql=json.dumps(canonical, ensure_ascii=False, default=str),
        operation=operation, collection=collection, spec=validated_spec,
        tables=[collection.lower()],
    )


def _validate_find(query: Any, spec: dict, max_rows: int) -> dict:
    filt = query if isinstance(query, dict) else {}
    _check_forbidden(filt, path="query")

    projection = spec.get("projection")
    if projection is not None and not isinstance(projection, dict):
        raise MongoRejected("projection must be an object if provided.")

    sort = spec.get("sort")
    if sort is not None and not isinstance(sort, dict):
        raise MongoRejected("sort must be an object if provided.")

    limit = spec.get("limit")
    if not isinstance(limit, int) or limit <= 0 or limit > max_rows:
        limit = max_rows

    return {"filter": filt, "projection": projection, "sort": sort, "limit": limit}


def _validate_aggregate(
    query: Any, collection: str, known_tables: set[str], max_rows: int
) -> list[dict]:
    if not isinstance(query, list):
        raise MongoRejected("For 'aggregate', query must be an array of pipeline stages.")

    pipeline: list[dict] = []
    has_limit = False

    for i, stage in enumerate(query):
        if not isinstance(stage, dict) or len(stage) != 1:
            raise MongoRejected(
                f"Pipeline stage {i} must be an object with exactly one key "
                "(the stage operator), e.g. {\"$match\": {...}}."
            )
        (stage_name, stage_body), = stage.items()
        if stage_name not in ALLOWED_STAGES:
            raise MongoRejected(
                f"Pipeline stage '{stage_name}' is not allowed. "
                f"Allowed stages: {', '.join(sorted(ALLOWED_STAGES))}."
            )
        _check_forbidden(stage_body, path=f"stage[{i}].{stage_name}")

        if stage_name == "$lookup" and isinstance(stage_body, dict):
            target = stage_body.get("from")
            if target and known_tables and str(target).lower() not in known_tables:
                raise MongoRejected(
                    f"$lookup targets unknown collection '{target}'."
                )
        if stage_name == "$limit":
            has_limit = True
            if not isinstance(stage_body, int) or stage_body > max_rows:
                stage = {"$limit": max_rows}

        pipeline.append(stage)

    if not has_limit:
        pipeline.append({"$limit": max_rows})
    else:
        # clamp an already-present $limit if it exceeds the cap
        pipeline = [
            {"$limit": max_rows}
            if "$limit" in s and isinstance(s.get("$limit"), int) and s["$limit"] > max_rows
            else s
            for s in pipeline
        ]

    return pipeline


def _check_forbidden(node: Any, *, path: str) -> None:
    """Recursively walk filters/expressions for forbidden keys — catches
    $where or $function nested inside $match, $project expressions, etc.,
    not just at the top level of a stage."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in FORBIDDEN_KEYS:
                raise MongoRejected(
                    f"'{k}' is blocked by policy (found at {path}.{k}). "
                    "Arbitrary-code and write operators are never permitted."
                )
            _check_forbidden(v, path=f"{path}.{k}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _check_forbidden(item, path=f"{path}[{i}]")
