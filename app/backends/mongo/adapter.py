"""Mongo backend adapter.

Composes engine.py (read-only enforcement) + validator.py (firewall) +
prompts.py (system prompt + tool) + executor.py (capped execution) behind
the BackendAdapter contract — the same shape backends/sql.py uses for SQL.
"""
from __future__ import annotations

import json
from typing import Any

from app.backends.mongo.executor import MongoExecutor, QueryResult
from app.backends.mongo.prompts import EXECUTE_MONGO_TOOL, build_mongo_system_prompt
from app.backends.mongo.validator import MongoRejected, ValidatedMongoQuery, validate_mongo_query


class MongoAdapter:
    """Wraps an already-built MongoExecutor behind the BackendAdapter contract."""

    RejectedError = MongoRejected

    def __init__(self, executor: MongoExecutor):
        self._executor = executor

    def validate(
        self, raw_query: str, *, dialect: str, known_tables: set[str], max_rows: int
    ) -> ValidatedMongoQuery:
        return validate_mongo_query(
            raw_query, dialect=dialect, known_tables=known_tables, max_rows=max_rows
        )

    def execute(self, validated_query: ValidatedMongoQuery) -> QueryResult:
        return self._executor.run(validated_query)

    def tool_schema(self) -> dict:
        return EXECUTE_MONGO_TOOL

    def parse_tool_input(self, tool_input: dict) -> str:
        # The model already gives us a structured object matching the tool
        # schema (operation/collection/query/...). Serialize it whole into
        # one canonical JSON string — the same "raw query as one string"
        # shape the orchestrator and validate_mongo_query both expect.
        return json.dumps(tool_input, ensure_ascii=False, default=str)

    def system_prompt(self, dialect: str, schema_text: str) -> str:
        return build_mongo_system_prompt(schema_text)

    def dialect_name(self, raw_backend: str) -> str:
        return "mongodb"

    def audit(self, event: str, query: str, **extra: Any) -> None:
        self._executor.audit(event, query, **extra)
