"""Tests for Hub.ask_stream — the multi-source streaming variant.

Each per-source QueryAgent.ask_stream is a real generator; here we drive
it with scripted fake Anthropic clients (as in test_hub.py) and assert the
Hub interleaves + brackets their frames correctly. The key invariants:
every forwarded source frame carries a `source` tag, and the lifecycle
events (plan, source_start, source_done, done) bracket them.
"""
import json
import types

import anthropic

from app.config import AppConfig, DatabaseCfg, SourceCfg
from app.hub import Hub


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _tool_use(tool_name, inp, tid="t1"):
    return types.SimpleNamespace(stop_reason="tool_use", content=[
        _Block(type="tool_use", id=tid, name=tool_name, input=inp),
    ])


def _end_turn(text):
    return types.SimpleNamespace(stop_reason="end_turn", content=[_Block(type="text", text=text)])


class _FakeClient:
    def __init__(self, scripts):
        self._scripts = scripts
        self._n = 0
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        i = min(self._n, len(self._scripts) - 1)
        self._n += 1
        return self._scripts[i](kwargs)


class _FakeFactory:
    def __init__(self, scripts_per_client):
        self._scripts_per_client = scripts_per_client
        self._n = 0

    def __call__(self, api_key):
        c = _FakeClient(self._scripts_per_client[self._n])
        self._n += 1
        return c


def _two_source_cfg():
    cfg = AppConfig()
    cfg.anthropic.api_key = "test-key"
    cfg.sources = [
        SourceCfg(name="shop", database=DatabaseCfg(url="mongomock://demo")),
        SourceCfg(name="reviews", database=DatabaseCfg(url="mongomock://other")),
    ]
    return cfg


def _collect(gen):
    """Drain an SSE generator into a list of (event, data) tuples."""
    out = []
    for frame in gen:
        event = "message"
        data = {}
        for line in frame.splitlines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        out.append((event, data))
    return out


def test_ask_stream_emits_full_lifecycle_and_tags_source_frames(monkeypatch):
    cfg = _two_source_cfg()
    # Each source agent: one successful aggregate then a final answer.
    shop_scripts = [
        lambda kw: _tool_use("execute_mongo_query", {
            "operation": "aggregate", "collection": "product_reviews",
            "query": [{"$count": "n"}],
        }),
        lambda kw: _end_turn("Shop has data."),
    ]
    reviews_scripts = [
        lambda kw: _tool_use("execute_mongo_query", {
            "operation": "find", "collection": "support_tickets", "query": {},
        }),
        lambda kw: _end_turn("Reviews has data."),
    ]
    hub_scripts = [
        lambda kw: _end_turn('["shop","reviews"]'),         # planner
        lambda kw: _end_turn("Combined: both sources have data."),  # synthesizer
    ]
    monkeypatch.setattr(anthropic, "Anthropic",
                        _FakeFactory([shop_scripts, reviews_scripts, hub_scripts]))

    hub = Hub(cfg)
    try:
        events = _collect(hub.ask_stream("how is everything?"))
    finally:
        hub.close()

    types_seen = [e for e, _ in events]
    assert types_seen[0] == "plan"
    assert types_seen.count("source_start") == 2
    assert types_seen.count("source_done") == 2
    assert "synthesizing" in types_seen
    assert types_seen[-1] == "done"

    # plan lists both sources
    plan_data = next(d for e, d in events if e == "plan")
    assert set(plan_data["sources"]) == {"shop", "reviews"}

    # every forwarded sql/thinking/blocked frame carries a source tag
    for e, d in events:
        if e in ("sql", "thinking", "blocked", "error"):
            assert d.get("source") in {"shop", "reviews"}, f"{e} frame missing source tag"

    # final done has the synthesized answer + per-source breakdown
    done = events[-1][1]
    assert done["answer"] == "Combined: both sources have data."
    assert {s["name"] for s in done["sources"]} == {"shop", "reviews"}


def test_ask_stream_single_planned_source_skips_synthesis(monkeypatch):
    cfg = _two_source_cfg()
    shop_scripts = [lambda kw: _end_turn("Only shop answer.")]
    reviews_scripts = [lambda kw: _end_turn("should not run")]
    hub_scripts = [lambda kw: _end_turn('["shop"]')]  # planner picks one, no synth
    monkeypatch.setattr(anthropic, "Anthropic",
                        _FakeFactory([shop_scripts, reviews_scripts, hub_scripts]))

    hub = Hub(cfg)
    try:
        events = _collect(hub.ask_stream("how many products?"))
    finally:
        hub.close()

    types_seen = [e for e, _ in events]
    assert types_seen.count("source_start") == 1
    assert "synthesizing" not in types_seen
    assert events[-1][0] == "done"
    assert events[-1][1]["answer"] == "Only shop answer."
    assert events[-1][1]["planned_sources"] == ["shop"]


def test_ask_stream_one_source_failing_still_completes(monkeypatch):
    cfg = _two_source_cfg()

    def boom(kw):
        raise RuntimeError("source outage")

    shop_scripts = [boom]
    reviews_scripts = [lambda kw: _end_turn("Reviews fine.")]
    hub_scripts = [
        lambda kw: _end_turn('["shop","reviews"]'),
        lambda kw: _end_turn("Reviews available; shop was down."),
    ]
    monkeypatch.setattr(anthropic, "Anthropic",
                        _FakeFactory([shop_scripts, reviews_scripts, hub_scripts]))

    hub = Hub(cfg)
    try:
        events = _collect(hub.ask_stream("status?"))
    finally:
        hub.close()

    done = events[-1][1]
    by_name = {s["name"]: s for s in done["sources"]}
    assert by_name["shop"]["error"] is not None
    assert by_name["reviews"]["error"] is None
    # both sources still emitted a source_done
    assert [e for e, _ in events].count("source_done") == 2
