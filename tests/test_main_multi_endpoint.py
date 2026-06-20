"""Integration tests for the multi-source HTTP surface: /api/ask/multi,
and the guards that keep single-source and multi-source endpoints from
being called against the wrong deployment mode.

get_config() is module-level @lru_cache'd and read once by lifespan(), so
these tests bypass it entirely by monkeypatching app.main.get_config
directly to a lambda returning a hand-built multi-source AppConfig —
that's the only way to get a different config per test without fighting
the cache.
"""
import types

import anthropic
from fastapi.testclient import TestClient

import app.main as main_module
from app.config import AppConfig, DatabaseCfg, SourceCfg


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _end_turn(text: str):
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
        client = _FakeClient(self._scripts_per_client[self._n])
        self._n += 1
        return client


def _multi_source_cfg():
    cfg = AppConfig()
    cfg.anthropic.api_key = "test-key"
    cfg.sources = [
        SourceCfg(name="shop", database=DatabaseCfg(url="mongomock://demo")),
        SourceCfg(name="reviews", database=DatabaseCfg(url="mongomock://other")),
    ]
    return cfg


def test_ask_multi_endpoint_returns_combined_answer(monkeypatch):
    monkeypatch.setattr(main_module, "get_config", lambda: _multi_source_cfg())
    monkeypatch.setattr(anthropic, "Anthropic", _FakeFactory([
        [lambda kw: _end_turn("89 products.")],
        [lambda kw: _end_turn("4.1 average rating.")],
        [
            lambda kw: _end_turn('["shop","reviews"]'),
            lambda kw: _end_turn("89 products, averaging 4.1 stars."),
        ],
    ]))

    with TestClient(main_module.app) as client:
        r = client.post("/api/ask/multi", json={"question": "how many products and rating?"})
        assert r.status_code == 200
        body = r.json()
        assert body["answer"] == "89 products, averaging 4.1 stars."
        assert set(body["planned_sources"]) == {"shop", "reviews"}
        assert {s["name"] for s in body["sources"]} == {"shop", "reviews"}


def test_ask_multi_returns_400_when_not_configured_for_multi_source():
    # default app config (no monkeypatch) is single-source
    with TestClient(main_module.app) as client:
        r = client.post("/api/ask/multi", json={"question": "anything"})
        assert r.status_code == 400
        assert "multi-source" in r.json()["detail"]


def test_ask_single_source_returns_400_when_configured_for_multi_source(monkeypatch):
    monkeypatch.setattr(main_module, "get_config", lambda: _multi_source_cfg())
    monkeypatch.setattr(anthropic, "Anthropic", _FakeFactory([[], [], []]))

    with TestClient(main_module.app) as client:
        r = client.post("/api/ask", json={"question": "anything"})
        assert r.status_code == 400
        assert "multi-source" in r.json()["detail"]


def test_health_reports_multi_source_mode(monkeypatch):
    monkeypatch.setattr(main_module, "get_config", lambda: _multi_source_cfg())
    monkeypatch.setattr(anthropic, "Anthropic", _FakeFactory([[], [], []]))

    with TestClient(main_module.app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["dialect"] == "multi-source"
        assert set(body["sources"]) == {"shop", "reviews"}


def test_schema_endpoint_returns_per_source_in_multi_mode(monkeypatch):
    monkeypatch.setattr(main_module, "get_config", lambda: _multi_source_cfg())
    monkeypatch.setattr(anthropic, "Anthropic", _FakeFactory([[], [], []]))

    with TestClient(main_module.app) as client:
        r = client.get("/api/schema")
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"shop", "reviews"}
        assert "dialect" in body["shop"]


def test_ask_multi_stream_returns_sse_with_done_event(monkeypatch):
    monkeypatch.setattr(main_module, "get_config", lambda: _multi_source_cfg())
    monkeypatch.setattr(anthropic, "Anthropic", _FakeFactory([
        [lambda kw: _end_turn("shop answer")],
        [lambda kw: _end_turn("reviews answer")],
        [
            lambda kw: _end_turn('["shop","reviews"]'),
            lambda kw: _end_turn("combined answer"),
        ],
    ]))
    with TestClient(main_module.app) as client:
        r = client.post("/api/ask/multi/stream", json={"question": "status?"})
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        assert "event: plan" in r.text
        assert "event: done" in r.text
        assert "event: source_done" in r.text


def test_ask_multi_stream_returns_400_when_not_multi_source():
    with TestClient(main_module.app) as client:
        r = client.post("/api/ask/multi/stream", json={"question": "anything"})
        assert r.status_code == 400
        assert "multi-source" in r.json()["detail"]
