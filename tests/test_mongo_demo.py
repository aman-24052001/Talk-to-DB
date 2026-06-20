"""Tests for the bundled mongomock:// Mongo demo — the zero-setup
equivalent of the SQLite demo. Mirrors test_hardening.py's
test_demo_db_is_seeded_and_consistent in spirit.
"""
from app.backends.factory import build_backend
from app.backends.mongo.adapter import MongoAdapter
from app.config import AppConfig, infer_backend_type


def test_mongomock_scheme_infers_mongodb_type():
    assert infer_backend_type("mongomock://demo") == "mongodb"


def test_demo_mode_builds_and_seeds():
    cfg = AppConfig()
    cfg.database.url = "mongomock://demo"
    backend = build_backend(cfg)
    try:
        assert isinstance(backend.adapter, MongoAdapter)
        snapshot = backend.schema.get()
        names = snapshot.table_names
        assert "product_reviews" in names
        assert "support_tickets" in names
    finally:
        backend.close()


def test_demo_seed_ids_are_within_sql_demo_ranges():
    """References customer_id 1-200 / t_shirt_id 1-89 — the exact ranges
    scripts/create_demo_db.py produces — so a cross-source question has a
    real, joinable answer once the Hub exists."""
    cfg = AppConfig()
    cfg.database.url = "mongomock://demo"
    backend = build_backend(cfg)
    try:
        reviews = backend.schema._db.get_collection("product_reviews")  # noqa: SLF001
        docs = list(reviews.find({}))
        assert docs
        assert all(1 <= d["t_shirt_id"] <= 89 for d in docs)
        assert all(1 <= d["customer_id"] <= 200 for d in docs)
    finally:
        backend.close()


def test_demo_seed_is_idempotent_within_one_process():
    """Calling get_readonly_db/seed twice against the SAME mongomock client
    (as would happen if something re-resolved the db) must not double the
    data."""
    from app.backends.mongo.engine import build_mongo_client, get_readonly_db

    cfg = AppConfig()
    cfg.database.url = "mongomock://demo"
    client = build_mongo_client(cfg)
    rdb1 = get_readonly_db(client, cfg)
    count1 = rdb1.get_collection("support_tickets").count_documents({})
    rdb2 = get_readonly_db(client, cfg)  # same client, resolve again
    count2 = rdb2.get_collection("support_tickets").count_documents({})
    assert count1 == count2 == 40


def test_demo_reviews_have_sparse_priority_field():
    """priority is deliberately present on only some tickets — exercises
    the schema introspection's presence% inference against real demo data,
    not just the synthetic mongomock fixtures in test_mongo_adapter.py."""
    cfg = AppConfig()
    cfg.database.url = "mongomock://demo"
    backend = build_backend(cfg)
    try:
        snapshot = backend.schema.get()
        tickets = next(c for c in snapshot.tables if c.name == "support_tickets")
        priority = next(f for f in tickets.fields if f.name == "priority")
        assert 0 < priority.presence_pct < 100
    finally:
        backend.close()
