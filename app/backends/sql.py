"""SQL backend adapter.

Composes the existing, already-tested SQL modules (guardrails/validator.py,
agent/prompts.py, db/executor.py) behind the BackendAdapter shape. There is
no new logic in this file — every method is a direct passthrough. That's
deliberate: this step is a pure refactor, not a behavior change, so the
existing test suite is the regression gate for it.
"""
from __future__ import annotations

from typing import Any

from app.agent.prompts import EXECUTE_SQL_TOOL, build_system_prompt
from app.db.executor import QueryExecutor, QueryResult
from app.guardrails.validator import SQLRejected, ValidatedSQL, sqlglot_dialect, validate_sql


class SQLAdapter:
    """Wraps an already-built QueryExecutor behind the BackendAdapter contract."""

    RejectedError = SQLRejected

    def __init__(self, executor: QueryExecutor):
        self._executor = executor

    def validate(
        self, raw_query: str, *, dialect: str, known_tables: set[str], max_rows: int
    ) -> ValidatedSQL:
        return validate_sql(
            raw_query, dialect=dialect, known_tables=known_tables, max_rows=max_rows
        )

    def execute(self, validated_query: ValidatedSQL) -> QueryResult:
        return self._executor.run(validated_query.sql)

    def tool_schema(self) -> dict:
        return EXECUTE_SQL_TOOL

    def system_prompt(self, dialect: str, schema_text: str) -> str:
        return build_system_prompt(dialect, schema_text)

    def dialect_name(self, raw_backend: str) -> str:
        return sqlglot_dialect(raw_backend)

    def audit(self, event: str, query: str, **extra: Any) -> None:
        self._executor.audit(event, query, **extra)
