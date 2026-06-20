"""Multi-source Hub: Planner -> parallel QueryAgents -> Synthesizer.

This only matters once config.sources has 2+ entries — with 0 or 1, the
existing single-backend path in app/main.py / app/backends/factory.py is
used instead and this module isn't touched at all. With 2+, the Hub:

  1. Planner: asks Claude which configured source(s) are relevant to the
     question (one cheap call, given a one-line schema summary per source
     — not the full schema, to keep this call fast and small). Falls back
     to "ask every source" if parsing fails or the model returns nothing
     usable — better to over-include than to silently answer from zero
     sources.
  2. Fan-out: runs each chosen source's QueryAgent.ask() in parallel
     (ThreadPoolExecutor) — these are the exact same per-backend agents
     used in single-source mode, completely unaware they're part of a Hub.
  3. Synthesizer: if only one source was actually queried, its answer is
     returned as-is (no synthesis call — same "skip the LLM call when
     there's nothing to combine" discipline as the Planner). If 2+, one
     more Claude call composes a single answer citing which source
     supported which part.

Known limitation: no SSE/streaming variant yet — Hub.ask() is blocking
only. Multiplexing live progress across N parallel agents is a real
design problem (which source's "thinking" event fires when?) deliberately
left for whenever the single-source UI actually needs to render multi-
source progress.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import time
from dataclasses import dataclass, field

import anthropic

from app.agent.orchestrator import QueryAgent
from app.backends.factory import Backend, build_backend
from app.config import AppConfig

log = logging.getLogger("talk_to_db")

_PLANNER_MAX_TOKENS = 300
_SYNTH_MAX_TOKENS = 800


@dataclass
class SourceAnswer:
    name: str
    answer: str = ""
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    error: str | None = None


@dataclass
class HubAnswer:
    answer: str
    planned_sources: list[str]
    sources: list[SourceAnswer]
    elapsed_ms: int = 0


class Hub:
    def __init__(self, cfg: AppConfig):
        if not cfg.resolved_api_key:
            raise RuntimeError(
                "No Anthropic API key. Put it in config.yaml under anthropic.api_key "
                "or export ANTHROPIC_API_KEY."
            )
        if not cfg.sources:
            raise ValueError("Hub requires config.sources to have at least one entry.")

        self._cfg = cfg
        self._backends: dict[str, Backend] = {}
        self._agents: dict[str, QueryAgent] = {}
        for src in cfg.sources:
            backend = build_backend(cfg, src.database)
            self._backends[src.name] = backend
            self._agents[src.name] = QueryAgent(cfg, backend.schema, backend.executor, backend.adapter)
        self._client = anthropic.Anthropic(api_key=cfg.resolved_api_key)

    def close(self) -> None:
        for backend in self._backends.values():
            backend.close()

    @property
    def source_names(self) -> list[str]:
        return list(self._agents.keys())

    def schemas(self, force: bool = False) -> dict[str, dict]:
        """Per-source introspected schema, in the same shape SchemaService.
        get().to_api() already returns for single-source mode."""
        return {name: b.schema.get(force=force).to_api() for name, b in self._backends.items()}

    # ── planning ─────────────────────────────────────────────────────────
    def _one_line_summaries(self) -> dict[str, str]:
        out = {}
        for name, backend in self._backends.items():
            snap = backend.schema.get()
            entities = ", ".join(sorted(snap.table_names)) or "(no tables/collections visible)"
            out[name] = f"[{snap.dialect}] {entities}"
        return out

    def plan(self, question: str) -> list[str]:
        names = self.source_names
        if len(names) <= 1:
            return names  # nothing to plan — only one source exists

        summaries = self._one_line_summaries()
        listing = "\n".join(f"- {n}: {summaries[n]}" for n in names)
        prompt = (
            "You are routing a question to one or more data sources.\n\n"
            f"Sources:\n{listing}\n\n"
            f'Question: "{question}"\n\n'
            "Which source(s) are needed to answer it? Respond with ONLY a "
            'JSON array of source names, e.g. ["orders_db"] or '
            '["orders_db","reviews_db"]. Include every source that holds '
            "data the question needs; do not guess at sources not listed."
        )
        try:
            resp = self._client.messages.create(
                model=self._cfg.anthropic.model,
                max_tokens=_PLANNER_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            chosen = json.loads(text)
            chosen = [n for n in chosen if n in names]
            if chosen:
                return chosen
        except Exception:
            log.warning("Hub planner call failed or returned unusable output", exc_info=True)
        return names  # safe fallback: ask everything rather than answer from nothing

    # ── fan-out ──────────────────────────────────────────────────────────
    def _ask_one(self, name: str, question: str, history: list[dict] | None) -> SourceAnswer:
        try:
            r = self._agents[name].ask(question, history)
            return SourceAnswer(
                name=name, answer=r.answer, sql=r.sql, columns=r.columns,
                rows=r.rows, row_count=r.row_count,
            )
        except Exception as e:
            log.warning("source '%s' failed", name, exc_info=True)
            return SourceAnswer(name=name, error=str(e))

    # ── synthesis ────────────────────────────────────────────────────────
    def _synthesize(self, question: str, answers: list[SourceAnswer]) -> str:
        parts = []
        for a in answers:
            if a.error:
                parts.append(f"Source '{a.name}': FAILED — {a.error}")
            else:
                parts.append(f"Source '{a.name}': {a.answer}")
        prompt = (
            f'Original question: "{question}"\n\n'
            "Each data source below was queried independently and returned "
            "its own answer:\n\n" + "\n\n".join(parts) + "\n\n"
            "Compose ONE final answer to the original question, combining "
            "what's relevant from each source. Briefly note which source "
            "supported which part if it adds clarity. If a source failed, "
            "answer from the sources that worked and mention the gap."
        )
        resp = self._client.messages.create(
            model=self._cfg.anthropic.model,
            max_tokens=_SYNTH_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    # ── entrypoint ───────────────────────────────────────────────────────
    def ask(self, question: str, history: list[dict] | None = None) -> HubAnswer:
        start = time.perf_counter()
        chosen = self.plan(question)

        results: dict[str, SourceAnswer] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(chosen))) as pool:
            futures = {pool.submit(self._ask_one, name, question, history): name for name in chosen}
            for fut in concurrent.futures.as_completed(futures):
                name = futures[fut]
                results[name] = fut.result()
        ordered = [results[n] for n in chosen]

        ok = [a for a in ordered if not a.error]
        if len(ok) == 1 and len(ordered) == 1:
            final_answer = ok[0].answer
        elif not ok:
            final_answer = "All queried sources failed: " + "; ".join(
                f"{a.name}: {a.error}" for a in ordered
            )
        else:
            final_answer = self._synthesize(question, ordered)

        return HubAnswer(
            answer=final_answer, planned_sources=chosen, sources=ordered,
            elapsed_ms=int((time.perf_counter() - start) * 1000),
        )
