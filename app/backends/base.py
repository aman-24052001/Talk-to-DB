"""The backend adapter contract.

This is step 1 of making Talk-to-DB datastore-agnostic. The agent loop in
app/agent/orchestrator.py is written against this Protocol only — it no
longer imports sqlglot, SQLAlchemy, or anything SQL-specific directly.

Adding a new backend (Mongo, etc.) means writing a new module that
satisfies this Protocol. It does not mean touching the orchestrator.

This file intentionally does NOT rename any existing concepts yet
(dialect / known_tables / sql are still SQL-flavored parameter names).
That generalization is a deliberate later step, done once a second
adapter actually exists to design the generic names against — renaming
now, with only one implementation to look at, would be guessing.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BackendAdapter(Protocol):
    """What the orchestrator needs from any datastore backend.

    RejectedError: the exception type this adapter raises from `validate()`
    when a query fails policy. Adapters expose it as a class attribute
    (e.g. `SQLAdapter.RejectedError = SQLRejected`) so the orchestrator can
    catch it generically: `except self._adapter.RejectedError as e:`
    """

    RejectedError: type[Exception]

    def validate(
        self, raw_query: str, *, dialect: str, known_tables: set[str], max_rows: int
    ) -> Any:
        """Parse + police a raw query. Returns a validated/rewritten query
        object on success. Raises `self.RejectedError` on any policy violation."""
        ...

    def execute(self, validated_query: Any) -> Any:
        """Run an already-validated query. Returns a QueryResult-shaped object
        (ok, columns, rows, row_count, truncated, elapsed_ms, error)."""
        ...

    def tool_schema(self) -> dict:
        """The Anthropic tool definition the model calls to query this backend."""
        ...

    def system_prompt(self, dialect: str, schema_text: str) -> str:
        """Build the system prompt fragment for this backend's query language."""
        ...

    def dialect_name(self, raw_backend: str) -> str:
        """Map the raw backend identifier (e.g. SQLAlchemy's url.get_backend_name())
        to whatever dialect string this adapter's validator/prompt expect."""
        ...

    def audit(self, event: str, query: str, **extra: Any) -> None:
        """Append one line to the audit log for this query attempt."""
        ...
