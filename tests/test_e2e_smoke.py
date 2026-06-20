"""End-to-end smoke test with a scripted fake Anthropic client.

Verifies the full path: POST /api/ask → agent loop → SQL firewall →
read-only executor → real demo.db → JSON response. Also verifies the
self-correction path when the model first tries something forbidden.
"""
import types

from fastapi.testclient import TestClient


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeAnthropicClient:
    """Turn 1: tries a DELETE (must be firewall-blocked and fed back).
    Turn 2: corrects to a SELECT. Turn 3: final text answer."""

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
            last = kwargs["messages"][-1]["content"][0]["content"]
            assert "REJECTED by firewall" in last
            return types.SimpleNamespace(stop_reason="tool_use", content=[
                _Block(type="tool_use", id="t2", name="execute_sql",
                       input={"sql": "SELECT brand, SUM(stock_quantity) AS stock "
                                     "FROM t_shirts GROUP BY brand ORDER BY stock DESC"}),
            ])
        return types.SimpleNamespace(stop_reason="end_turn", content=[
            _Block(type="text", text="Nike has the most stock."),
        ])


def test_full_ask_flow(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: FakeAnthropicClient())

    from app.main import app
    with TestClient(app) as client:
        schema = client.get("/api/schema").json()
        assert {t["name"] for t in schema["tables"]} >= {"t_shirts", "orders"}

        r = client.post("/api/ask", json={"question": "Which brand has most stock?"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["answer"] == "Nike has the most stock."
        assert body["blocked"] == 1                      # the DELETE was stopped
        assert body["steps"][0]["kind"] == "blocked"
        assert body["steps"][1]["kind"] == "sql"
        assert body["sql"] and "select" in body["sql"].lower()
        # dual-field contract: the generic `query` field must be present and
        # mirror the deprecated `sql` alias, top-level and per-step.
        assert body["query"] == body["sql"]
        assert body["steps"][1]["query"] == body["steps"][1]["sql"]
        assert body["row_count"] >= 1 and body["columns"] == ["brand", "stock"]
