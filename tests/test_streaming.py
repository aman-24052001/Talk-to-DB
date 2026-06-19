"""Tests for SSE streaming endpoint and new v3 features."""
import json
import types

from fastapi.testclient import TestClient


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeClientStream:
    """
    Turn 1: tries DELETE (blocked).
    Turn 2: correct SELECT.
    Turn 3: final text answer.
    """
    def __init__(self):
        self.calls = 0
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return types.SimpleNamespace(stop_reason="tool_use", content=[
                _Block(type="tool_use", id="t1", name="execute_sql",
                       input={"sql": "DELETE FROM orders"}),
            ])
        if self.calls == 2:
            return types.SimpleNamespace(stop_reason="tool_use", content=[
                _Block(type="tool_use", id="t2", name="execute_sql",
                       input={"sql": "SELECT COUNT(*) AS total FROM orders"}),
            ])
        return types.SimpleNamespace(stop_reason="end_turn", content=[
            _Block(type="text", text="There are orders in the database."),
        ])


def _parse_sse(text: str) -> list[dict]:
    """Parse raw SSE text into list of {event, data} dicts."""
    events = []
    for block in text.strip().split("\n\n"):
        event_type = "message"
        data_str = ""
        for line in block.strip().splitlines():
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                data_str = line[6:].strip()
        if data_str:
            try:
                events.append({"event": event_type, "data": json.loads(data_str)})
            except json.JSONDecodeError:
                pass
    return events


def test_sse_stream_endpoint(monkeypatch):
    """SSE endpoint yields thinking/blocked/sql/done events in order."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: FakeClientStream())

    from app.main import app
    with TestClient(app) as client:
        r = client.post(
            "/api/ask/stream",
            json={"question": "How many orders?"},
            headers={"Accept": "text/event-stream"},
        )
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")

        events = _parse_sse(r.text)
        event_types = [e["event"] for e in events]

        assert "thinking" in event_types
        assert "blocked"  in event_types   # DELETE was rejected
        assert "sql"      in event_types   # SELECT was executed
        assert "done"     in event_types   # final answer

        done = next(e["data"] for e in events if e["event"] == "done")
        assert done["answer"] == "There are orders in the database."
        assert done["blocked"] == 1
        assert done["turns"] == 3
        assert done["sql"] is not None


def test_sse_blocked_event_has_reason(monkeypatch):
    """Blocked SSE event contains the firewall rejection reason."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import anthropic

    class _OneBlock:
        def __init__(self):
            self.calls = 0
            self.messages = types.SimpleNamespace(create=self._create)
        def _create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return types.SimpleNamespace(stop_reason="tool_use", content=[
                    _Block(type="tool_use", id="t1", name="execute_sql",
                           input={"sql": "DROP TABLE customers"}),
                ])
            return types.SimpleNamespace(stop_reason="end_turn", content=[
                _Block(type="text", text="Cannot do that."),
            ])

    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: _OneBlock())
    from app.main import app
    with TestClient(app) as client:
        r = client.post("/api/ask/stream", json={"question": "drop customers"})
        events = _parse_sse(r.text)
        blocked = [e for e in events if e["event"] == "blocked"]
        assert blocked, "Expected at least one blocked event"
        assert blocked[0]["data"]["reason"]   # has a reason string
        assert "DROP" in blocked[0]["data"]["sql"].upper()


def test_blocking_ask_still_works(monkeypatch):
    """Original /api/ask blocking endpoint still returns correct AskResponse."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: FakeClientStream())

    from app.main import app
    with TestClient(app) as client:
        r = client.post("/api/ask", json={"question": "How many orders?"})
        assert r.status_code == 200
        body = r.json()
        assert body["answer"] == "There are orders in the database."
        assert body["blocked"] == 1
        assert body["sql"] is not None


def test_history_turns_respected(monkeypatch):
    """history_turns config limits how many history items are sent to the model."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import anthropic

    received_messages = []

    class _Inspector:
        def __init__(self):
            self.messages = types.SimpleNamespace(create=self._create)
        def _create(self, **kwargs):
            received_messages.append(kwargs.get("messages", []))
            return types.SimpleNamespace(stop_reason="end_turn", content=[
                _Block(type="text", text="ok"),
            ])

    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: _Inspector())
    # Override config to use history_turns=2
    from app import config as cfg_mod
    cfg_mod.get_config.cache_clear()
    original_cfg = cfg_mod.get_config()

    # Build 10 history items, only 2 should be kept
    hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg{i}"} for i in range(10)]

    from app.agent.orchestrator import SQLAgent
    from app.db.executor import QueryExecutor
    from app.db.introspect import SchemaService
    from app.db.engine import build_engine

    cfg = cfg_mod.AppConfig()
    cfg.guardrails.history_turns = 2
    cfg.anthropic.api_key = "test"
    cfg.database.url = original_cfg.database.url

    engine = build_engine(cfg)
    schema = SchemaService(engine, cfg)
    executor = QueryExecutor(engine, cfg)
    agent = SQLAgent(cfg, schema, executor)
    agent.ask("test question", hist)

    # The messages sent to Anthropic should be ≤ 3 (2 history + 1 current question)
    assert len(received_messages) > 0
    sent = received_messages[0]
    # last item is always the current question; items before are history
    history_items = sent[:-1]
    assert len(history_items) <= 2
    executor.shutdown()
    engine.dispose()


def test_executor_shutdown_is_callable():
    """B1: executor.shutdown() method exists and runs without error."""
    from app.config import AppConfig
    from app.db.engine import build_engine
    from app.db.executor import QueryExecutor

    cfg = AppConfig()
    engine = build_engine(cfg)
    ex = QueryExecutor(engine, cfg)
    ex.shutdown()   # must not raise
    engine.dispose()
