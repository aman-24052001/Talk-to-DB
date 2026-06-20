"""Tests for app/hub.py — the multi-source Planner/fan-out/Synthesizer.

Each QueryAgent the Hub builds constructs its OWN anthropic.Anthropic
client, and the Hub builds one more for planning/synthesis. _FakeFactory
below returns one independently-scripted fake client per construction,
in construction order: source agents first (in config order), then the
Hub's own client last — that's the exact order Hub.__init__ creates them in.
"""
import types

import anthropic
import pytest

from app.config import AppConfig, DatabaseCfg, SourceCfg
from app.hub import Hub


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _end_turn(text: str):
    return types.SimpleNamespace(stop_reason="end_turn", content=[_Block(type="text", text=text)])


class _FakeClient:
    """One fake anthropic.Anthropic instance, scripted by a list of
    response-producing callables, one per call (last one repeats)."""

    def __init__(self, scripts):
        self._scripts = scripts
        self._n = 0
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        i = min(self._n, len(self._scripts) - 1)
        self._n += 1
        return self._scripts[i](kwargs)


class _FakeFactory:
    """Hands out one _FakeClient per anthropic.Anthropic(...) construction,
    in order: scripts_per_client[0] for the 1st construction, etc."""

    def __init__(self, scripts_per_client):
        self._scripts_per_client = scripts_per_client
        self._n = 0

    def __call__(self, api_key):
        client = _FakeClient(self._scripts_per_client[self._n])
        self._n += 1
        return client


def _two_source_cfg():
    cfg = AppConfig()
    cfg.anthropic.api_key = "test-key"
    cfg.sources = [
        SourceCfg(name="shop", database=DatabaseCfg(url="mongomock://demo")),
        SourceCfg(name="reviews", database=DatabaseCfg(url="mongomock://other")),
    ]
    return cfg


# ---------------------------------------------------------------- single-source passthrough
def test_single_source_skips_planner_call(monkeypatch):
    cfg = AppConfig()
    cfg.anthropic.api_key = "test-key"
    cfg.sources = [SourceCfg(name="only", database=DatabaseCfg(url="mongomock://demo"))]

    monkeypatch.setattr(
        anthropic, "Anthropic",
        _FakeFactory([
            [lambda kw: _end_turn("There are 89 products.")],  # source agent
            [],  # Hub's own client — never called for a single source, but still constructed
        ]),
    )
    hub = Hub(cfg)
    try:
        assert hub.plan("how many products?") == ["only"]
        result = hub.ask("how many products?")
        assert result.answer == "There are 89 products."
        assert result.planned_sources == ["only"]
    finally:
        hub.close()


# ---------------------------------------------------------------- multi-source plan + synth
def test_multi_source_plans_fans_out_and_synthesizes(monkeypatch):
    cfg = _two_source_cfg()

    monkeypatch.setattr(anthropic, "Anthropic", _FakeFactory([
        [lambda kw: _end_turn("There are 89 products in stock.")],      # shop agent
        [lambda kw: _end_turn("Average rating across products is 4.1.")],  # reviews agent
        [  # Hub's own client: call 0 = planner, call 1 = synthesizer
            lambda kw: _end_turn('["shop","reviews"]'),
            lambda kw: _end_turn("There are 89 products, averaging a 4.1 rating."),
        ],
    ]))
    hub = Hub(cfg)
    try:
        result = hub.ask("How many products do we have and how are they rated?")
        assert set(result.planned_sources) == {"shop", "reviews"}
        assert len(result.sources) == 2
        assert result.answer == "There are 89 products, averaging a 4.1 rating."
        names = {s.name for s in result.sources}
        assert names == {"shop", "reviews"}
    finally:
        hub.close()


def test_planner_selects_single_relevant_source(monkeypatch):
    """When the planner decides only one source is relevant, the Hub
    shouldn't bother querying the other one OR calling the synthesizer."""
    cfg = _two_source_cfg()
    reviews_agent_was_called = {"yes": False}

    def reviews_script(kw):
        reviews_agent_was_called["yes"] = True
        return _end_turn("should not be called")

    monkeypatch.setattr(anthropic, "Anthropic", _FakeFactory([
        [lambda kw: _end_turn("89 products.")],   # shop agent
        [reviews_script],                          # reviews agent (should be skipped)
        [lambda kw: _end_turn('["shop"]')],         # Hub: planner only, no synth needed
    ]))
    hub = Hub(cfg)
    try:
        result = hub.ask("how many products do we have?")
        assert result.planned_sources == ["shop"]
        assert result.answer == "89 products."   # passthrough, no synthesis call
        assert reviews_agent_was_called["yes"] is False
    finally:
        hub.close()


def test_planner_falls_back_to_all_sources_on_bad_json(monkeypatch):
    cfg = _two_source_cfg()
    monkeypatch.setattr(anthropic, "Anthropic", _FakeFactory([
        [lambda kw: _end_turn("shop answer")],
        [lambda kw: _end_turn("reviews answer")],
        [
            lambda kw: _end_turn("not valid json at all"),         # planner: garbage
            lambda kw: _end_turn("combined answer from both"),     # synth still runs over both
        ],
    ]))
    hub = Hub(cfg)
    try:
        result = hub.ask("anything")
        assert set(result.planned_sources) == {"shop", "reviews"}
    finally:
        hub.close()


# ---------------------------------------------------------------- partial failure
def test_one_source_failing_does_not_crash_the_hub(monkeypatch):
    cfg = _two_source_cfg()

    def broken_agent_script(kw):
        raise RuntimeError("simulated source outage")

    monkeypatch.setattr(anthropic, "Anthropic", _FakeFactory([
        [broken_agent_script],                       # shop agent: blows up
        [lambda kw: _end_turn("reviews are fine")],   # reviews agent: works
        [
            lambda kw: _end_turn('["shop","reviews"]'),
            lambda kw: _end_turn("Reviews data available; shop source was unavailable."),
        ],
    ]))
    hub = Hub(cfg)
    try:
        result = hub.ask("how is everything?")
        by_name = {s.name: s for s in result.sources}
        assert by_name["shop"].error is not None
        assert by_name["reviews"].error is None
        assert "unavailable" in result.answer.lower() or "Reviews data" in result.answer
    finally:
        hub.close()


def test_all_sources_failing_returns_explanatory_answer_without_a_synth_call(monkeypatch):
    cfg = _two_source_cfg()

    def boom(kw):
        raise RuntimeError("down")

    monkeypatch.setattr(anthropic, "Anthropic", _FakeFactory([
        [boom], [boom],
        [lambda kw: _end_turn('["shop","reviews"]')],  # planner only — no synth call should fire
    ]))
    hub = Hub(cfg)
    try:
        result = hub.ask("anything")
        assert "failed" in result.answer.lower()
        assert all(s.error for s in result.sources)
    finally:
        hub.close()


# ---------------------------------------------------------------- construction guards
def test_hub_requires_at_least_one_source():
    cfg = AppConfig()
    cfg.anthropic.api_key = "test-key"
    with pytest.raises(ValueError, match="sources"):
        Hub(cfg)


def test_hub_requires_api_key():
    cfg = AppConfig()
    cfg.sources = [SourceCfg(name="only", database=DatabaseCfg(url="mongomock://demo"))]
    with pytest.raises(RuntimeError, match="API key"):
        Hub(cfg)
