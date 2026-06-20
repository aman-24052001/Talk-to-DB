"""API contracts.

Field-naming note: every place that exposes the generated query carries
BOTH `query` (the backend-agnostic name — literal SQL for SQL backends, a
canonical JSON spec for Mongo) and `sql` (the original name, kept as a
deprecated alias so existing clients don't break). Producers set `sql`;
the `_mirror_query` validators copy it into `query` automatically, so the
two never drift. `sql` will be removed in a future major version.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class HistoryTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=4000)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    history: list[HistoryTurn] = Field(default_factory=list, max_length=12)


class StepOut(BaseModel):
    kind: str
    query: str = ""
    sql: str = ""        # deprecated alias for `query`
    detail: str = ""
    rows: int = 0
    elapsed_ms: int = 0

    @model_validator(mode="after")
    def _mirror_query(self) -> "StepOut":
        if not self.query and self.sql:
            self.query = self.sql
        elif self.query and not self.sql:
            self.sql = self.query
        return self


class AskResponse(BaseModel):
    answer: str
    query: str | None = None
    sql: str | None = None    # deprecated alias for `query`
    columns: list[str]
    rows: list[list]
    row_count: int
    truncated: bool
    steps: list[StepOut]
    turns: int
    blocked: int
    elapsed_ms: int
    model: str

    @model_validator(mode="after")
    def _mirror_query(self) -> "AskResponse":
        if self.query is None and self.sql is not None:
            self.query = self.sql
        elif self.query is not None and self.sql is None:
            self.sql = self.query
        return self


class SourceAskResult(BaseModel):
    name: str
    answer: str
    query: str | None = None
    sql: str | None = None    # deprecated alias for `query`
    columns: list[str] = Field(default_factory=list)
    rows: list[list] = Field(default_factory=list)
    row_count: int = 0
    error: str | None = None

    @model_validator(mode="after")
    def _mirror_query(self) -> "SourceAskResult":
        if self.query is None and self.sql is not None:
            self.query = self.sql
        elif self.query is not None and self.sql is None:
            self.sql = self.query
        return self


class MultiAskResponse(BaseModel):
    answer: str
    planned_sources: list[str]
    sources: list[SourceAskResult]
    elapsed_ms: int
