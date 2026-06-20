"""Tests for the Mongo adapter stack (engine read-only wrapper, schema
introspection, executor) using mongomock — an in-memory fake server, no
real MongoDB needed. Mirrors test_hardening.py's spirit: prove the
guarantees hold against a real(ish) backend, not just the validator logic
in isolation.
"""
import json

import mongomock
import pytest

from app.backends.mongo.adapter import MongoAdapter
from app.backends.mongo.engine import ReadOnlyCollection, ReadOnlyDatabase
from app.backends.mongo.executor import MongoExecutor
from app.backends.mongo.introspect import MongoSchemaService
from app.config import AppConfig


def _seeded_db():
    client = mongomock.MongoClient()
    db = client["testdb"]
    db.orders.insert_many([
        {"customer": "alice", "status": "completed", "amount": 30},
        {"customer": "bob", "status": "completed", "amount": 20},
        {"customer": "alice", "status": "pending", "amount": 5},
    ])
    db.customers.insert_many([
        {"name": "alice", "city": "Bengaluru"},
        {"name": "bob", "city": "Pune"},
    ])
    # sparse field on purpose, to test presence% inference
    db.tickets.insert_many([
        {"subject": "Login issue", "priority": "high"},
        {"subject": "Refund request"},
    ])
    return ReadOnlyDatabase(db)


# ---------------------------------------------------------------- read-only capability narrowing
def test_readonly_collection_has_no_write_methods():
    rdb = _seeded_db()
    coll = rdb.get_collection("orders")
    assert isinstance(coll, ReadOnlyCollection)
    for forbidden in ("insert_one", "insert_many", "update_one", "update_many",
                      "delete_one", "delete_many", "replace_one", "drop",
                      "create_index", "rename"):
        assert not hasattr(coll, forbidden), f"{forbidden} must not be reachable"


def test_readonly_database_has_no_admin_methods():
    rdb = _seeded_db()
    for forbidden in ("command", "create_collection", "drop_collection"):
        assert not hasattr(rdb, forbidden), f"{forbidden} must not be reachable"


def test_readonly_collection_read_methods_work():
    rdb = _seeded_db()
    coll = rdb.get_collection("orders")
    assert coll.count_documents({"status": "completed"}) == 2
    assert coll.estimated_document_count() == 3
    assert sorted(coll.distinct("customer")) == ["alice", "bob"]


# ---------------------------------------------------------------- introspection
def test_schema_introspection_infers_fields_and_presence():
    cfg = AppConfig()
    cfg.guardrails.sample_rows_in_schema = 3
    schema = MongoSchemaService(_seeded_db(), cfg)
    snapshot = schema.get()

    assert snapshot.dialect == "mongodb"
    names = {c.name for c in snapshot.tables}
    assert {"orders", "customers", "tickets"} <= names

    tickets = next(c for c in snapshot.tables if c.name == "tickets")
    subject_field = next(f for f in tickets.fields if f.name == "subject")
    priority_field = next(f for f in tickets.fields if f.name == "priority")
    assert subject_field.presence_pct == 100   # every ticket has a subject
    assert priority_field.presence_pct == 50   # only one of two has priority


def test_schema_to_prompt_mentions_collections_and_presence():
    cfg = AppConfig()
    schema = MongoSchemaService(_seeded_db(), cfg)
    text = schema.get().to_prompt()
    assert "COLLECTION orders" in text
    assert "%" in text  # presence percentages rendered


def test_schema_respects_denied_tables():
    cfg = AppConfig()
    cfg.guardrails.denied_tables = ["customers"]
    schema = MongoSchemaService(_seeded_db(), cfg)
    names = schema.get().table_names
    assert "customers" not in names
    assert "orders" in names


# ---------------------------------------------------------------- adapter round trip
def test_adapter_find_round_trip():
    cfg = AppConfig()
    rdb = _seeded_db()
    executor = MongoExecutor(rdb, cfg)
    adapter = MongoAdapter(executor)
    try:
        raw = adapter.parse_tool_input({
            "operation": "find", "collection": "orders", "query": {"status": "completed"},
        })
        validated = adapter.validate(raw, dialect="mongodb",
                                      known_tables={"orders", "customers", "tickets"}, max_rows=200)
        result = adapter.execute(validated)
        assert result.ok
        assert result.row_count == 2
        assert "customer" in result.columns
    finally:
        executor.shutdown()


def test_adapter_aggregate_round_trip():
    cfg = AppConfig()
    rdb = _seeded_db()
    executor = MongoExecutor(rdb, cfg)
    adapter = MongoAdapter(executor)
    try:
        raw = adapter.parse_tool_input({
            "operation": "aggregate", "collection": "orders",
            "query": [
                {"$match": {"status": "completed"}},
                {"$group": {"_id": "$customer", "total": {"$sum": "$amount"}}},
                {"$sort": {"total": -1}},
            ],
        })
        validated = adapter.validate(raw, dialect="mongodb",
                                      known_tables={"orders", "customers", "tickets"}, max_rows=200)
        result = adapter.execute(validated)
        assert result.ok
        assert result.row_count == 2
        assert result.rows[0][result.columns.index("total")] == 30  # alice's completed order
    finally:
        executor.shutdown()


def test_adapter_rejects_write_attempt_before_touching_db():
    cfg = AppConfig()
    rdb = _seeded_db()
    executor = MongoExecutor(rdb, cfg)
    adapter = MongoAdapter(executor)
    raw = adapter.parse_tool_input({
        "operation": "aggregate", "collection": "orders",
        "query": [{"$out": "stolen_copy"}],
    })
    with pytest.raises(adapter.RejectedError):
        adapter.validate(raw, dialect="mongodb", known_tables={"orders"}, max_rows=200)
    # and the read-only proxy genuinely never got a write call:
    assert "stolen_copy" not in rdb.list_collection_names()
    executor.shutdown()


def test_adapter_audit_log_records_execution(tmp_path):
    cfg = AppConfig()
    cfg.logging.audit_file = str(tmp_path / "audit.log")
    rdb = _seeded_db()
    executor = MongoExecutor(rdb, cfg)
    adapter = MongoAdapter(executor)
    try:
        raw = adapter.parse_tool_input({"operation": "find", "collection": "orders", "query": {}})
        validated = adapter.validate(raw, dialect="mongodb", known_tables={"orders"}, max_rows=200)
        adapter.execute(validated)
    finally:
        executor.shutdown()

    lines = (tmp_path / "audit.log").read_text().strip().splitlines()
    assert lines
    record = json.loads(lines[-1])
    assert record["event"] == "executed"
    assert "orders" in record["sql"]


def test_adapter_parse_tool_input_is_a_string():
    cfg = AppConfig()
    executor = MongoExecutor(_seeded_db(), cfg)
    adapter = MongoAdapter(executor)
    raw = adapter.parse_tool_input({"operation": "find", "collection": "orders", "query": {}})
    assert isinstance(raw, str)
    json.loads(raw)  # must round-trip through JSON cleanly
    executor.shutdown()
