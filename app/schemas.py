"""API contracts."""
from __future__ import annotations

from pydantic import BaseModel, Field


class HistoryTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=4000)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    history: list[HistoryTurn] = Field(default_factory=list, max_length=12)


class StepOut(BaseModel):
    kind: str
    sql: str
    detail: str = ""
    rows: int = 0
    elapsed_ms: int = 0


class AskResponse(BaseModel):
    answer: str
    sql: str | None
    columns: list[str]
    rows: list[list]
    row_count: int
    truncated: bool
    steps: list[StepOut]
    turns: int
    blocked: int
    elapsed_ms: int
    model: str
