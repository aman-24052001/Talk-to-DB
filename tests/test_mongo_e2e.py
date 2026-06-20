"""End-to-end smoke test for the Mongo path: a scripted fake Anthropic
client driving the real QueryAgent loop against a mongomock-backed
MongoAdapter — verifies the same self-correction path test_e2e_smoke.py
proves for SQL (model tries something forbidden, gets rejected with a
reason, corrects, gets a real answer) but for Mongo's firewall instead.
"""
import json
import types

import mongomock

from app.agent.orchestrator import QueryAgent
from app.backends.mongo.adapter import MongoAdapter
from app.backends.mongo.engine import ReadOnlyDatabase
from app.backends.mongo.executor import MongoExecutor
from app.backends.mongo.introspect import MongoSchemaService
from app.config import AppConfig


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeMongoClient:
    """Turn 1: tries $out (must be firewall-blocked and fed back).
    Turn 2: corrects to a valid aggregate. Turn 3: final text answer."""

    def __init__(self):
        self.calls = 0
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return types.SimpleNamespace(stop_reason="tool_use", content=[
                _Block(type="tool_use", id="t1", name="execute_mongo_query", input={
                    "operation": "aggregate", "collection": "orders",
                    "query": [{"$out": "stolen_copy"}],
                }),
            ])
        if self.calls == 2:
            last = kwargs["messages"][-1]["content"][0]["content"]
            assert "REJECTED by firewall" in last
            return types.SimpleNamespace(stop_reason="tool_use", content=[
                _Block(type="tool_use", id="t2", name="execute_mongo_query", input={
                    "operation": "aggregate", "collection": "orders",
                    "query": [
                        {"$match": {"status": "completed"}},
                        {"$group": {"_id": "$customer", "total": {"$sum": "$amount"}}},
                        {"$sort": {"total": -1}},
                    ],
                }),
            ])
        return types.SimpleNamespace(stop_reason="end_turn", content=[
            _Block(type="text", text="Alice has the highest completed-order total."),
        ])


def _build_agent(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: FakeMongoClient())

    client = mongomock.MongoClient()
    db = client["testdb"]
    db.orders.insert_many([
        {"customer": "alice", "status": "completed", "amount": 30},
        {"customer": "bob", "status": "completed", "amount": 20},
        {"customer": "alice", "status": "pending", "amount": 5},
    ])
    rdb = ReadOnlyDatabase(db)

    cfg = AppConfig()
    cfg.anthropic.api_key = "test-key"
    schema = MongoSchemaService(rdb, cfg)
    executor = MongoExecutor(rdb, cfg)
    adapter = MongoAdapter(executor)
    agent = QueryAgent(cfg, schema, executor, adapter)
    return agent, executor


def test_full_ask_flow_against_mongo_backend(monkeypatch):
    agent, executor = _build_agent(monkeypatch)
    try:
        result = agent.ask("Which customer has the highest completed-order total?")
    finally:
        executor.shutdown()

    assert result.answer == "Alice has the highest completed-order total."
    assert result.blocked == 1                       # the $out attempt was stopped
    assert result.steps[0].kind == "blocked"
    assert result.steps[1].kind == "sql"              # field name kept for now, see base.py note
    assert result.sql and "aggregate" in result.sql
    assert result.row_count == 2
    parsed = json.loads(result.sql)
    assert parsed["collection"] == "orders"


def test_blocked_step_carries_the_out_attempt_for_visibility(monkeypatch):
    agent, executor = _build_agent(monkeypatch)
    try:
        result = agent.ask("dump orders to a new collection")
    finally:
        executor.shutdown()

    blocked_step = result.steps[0]
    assert blocked_step.kind == "blocked"
    assert "$out" in blocked_step.sql
