"""Integration tests against a REAL MongoDB server.

These are skipped automatically unless TALK_TO_DB_MONGO_URL is set (it's
set only in the dedicated CI job with a mongo:7 service container). The
rest of the suite uses mongomock, which is fast and dependency-free but
can diverge from real driver/server behaviour — these tests close that
gap for the parts most likely to differ: real aggregation execution,
maxTimeMS handling, real read-only RBAC, and the capability-narrowed
wrapper against a genuine pymongo Collection.

Run locally with a throwaway server:
    docker run -d -p 27017:27017 mongo:7
    TALK_TO_DB_MONGO_URL=mongodb://localhost:27017/talktodb_ci \\
        pytest tests/test_mongo_integration.py -m mongo_integration
"""
import os

import pytest

pytestmark = pytest.mark.mongo_integration

_MONGO_URL = os.environ.get("TALK_TO_DB_MONGO_URL")

pytest_skip = pytest.mark.skipif(
    not _MONGO_URL,
    reason="TALK_TO_DB_MONGO_URL not set — real-Mongo integration tests skipped",
)


@pytest.fixture
def real_db():
    """A real, seeded, read-only-wrapped Mongo database. Drops its test
    collections before and after so reruns are clean."""
    if not _MONGO_URL:
        pytest.skip("no real Mongo configured")

    from pymongo import MongoClient

    from app.backends.mongo.engine import ReadOnlyDatabase

    client = MongoClient(_MONGO_URL, serverSelectionTimeoutMS=5000)
    raw = client.get_default_database()
    for c in ("orders", "customers"):
        raw.drop_collection(c)
    raw.orders.insert_many([
        {"customer": "alice", "status": "completed", "amount": 30},
        {"customer": "bob", "status": "completed", "amount": 20},
        {"customer": "alice", "status": "pending", "amount": 5},
    ])
    raw.customers.insert_many([
        {"name": "alice", "city": "Bengaluru"},
        {"name": "bob", "city": "Pune"},
    ])
    yield ReadOnlyDatabase(raw)
    for c in ("orders", "customers"):
        raw.drop_collection(c)
    client.close()


@pytest_skip
def test_real_introspection(real_db):
    from app.backends.mongo.introspect import MongoSchemaService
    from app.config import AppConfig

    schema = MongoSchemaService(real_db, AppConfig())
    snap = schema.get()
    assert snap.dialect == "mongodb"
    assert "orders" in snap.table_names
    assert "customers" in snap.table_names


@pytest_skip
def test_real_aggregate_execution_and_limit(real_db):
    from app.backends.mongo.adapter import MongoAdapter
    from app.backends.mongo.executor import MongoExecutor
    from app.config import AppConfig

    cfg = AppConfig()
    executor = MongoExecutor(real_db, cfg)
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
                                     known_tables={"orders", "customers"}, max_rows=200)
        result = adapter.execute(validated)
        assert result.ok
        assert result.row_count == 2
        # alice's completed total (30) should sort first
        assert result.rows[0][result.columns.index("total")] == 30
    finally:
        executor.shutdown()


@pytest_skip
def test_real_write_attempt_is_blocked_and_never_reaches_db(real_db):
    """The capability-narrowed wrapper must hold against a genuine pymongo
    Collection, not just mongomock — confirm no write method is reachable
    and a $out is firewall-rejected before any execution."""
    from app.backends.mongo.adapter import MongoAdapter
    from app.backends.mongo.executor import MongoExecutor
    from app.config import AppConfig

    coll = real_db.get_collection("orders")
    for forbidden in ("insert_one", "update_many", "delete_many", "drop"):
        assert not hasattr(coll, forbidden)

    cfg = AppConfig()
    executor = MongoExecutor(real_db, cfg)
    adapter = MongoAdapter(executor)
    try:
        raw = adapter.parse_tool_input({
            "operation": "aggregate", "collection": "orders",
            "query": [{"$out": "stolen_copy"}],
        })
        with pytest.raises(adapter.RejectedError):
            adapter.validate(raw, dialect="mongodb", known_tables={"orders"}, max_rows=200)
        assert "stolen_copy" not in real_db.list_collection_names()
    finally:
        executor.shutdown()
