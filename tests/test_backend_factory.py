"""Tests for the backend factory (app/backends/factory.py) and the
QueryAgent rename/adapter-injection introduced alongside it.

These cover genuinely NEW behavior from this change — unlike the SQL
firewall/executor tests, nothing here was exercised before this commit.
"""
from app.agent.orchestrator import QueryAgent, SQLAgent
from app.backends.factory import Backend, build_backend
from app.backends.sql import SQLAdapter
from app.config import AppConfig, infer_backend_type


# ── URL-scheme inference ────────────────────────────────────────────────

def test_infer_backend_type_sql_urls():
    assert infer_backend_type("sqlite:///data/demo.db") == "sql"
    assert infer_backend_type("postgresql+psycopg2://u:p@host/db") == "sql"
    assert infer_backend_type("mysql+pymysql://u:p@host/db") == "sql"


def test_infer_backend_type_mongo_urls():
    assert infer_backend_type("mongodb://localhost:27017/db") == "mongodb"
    assert infer_backend_type("mongodb+srv://cluster.example.net/db") == "mongodb"


def test_resolved_database_type_defaults_to_inference():
    cfg = AppConfig()  # default url is sqlite:///...
    assert cfg.database.type is None
    assert cfg.resolved_database_type == "sql"


def test_resolved_database_type_explicit_override_wins():
    cfg = AppConfig()
    cfg.database.type = "mongodb"
    assert cfg.resolved_database_type == "mongodb"


# ── build_backend() ──────────────────────────────────────────────────────

def test_build_backend_sql_returns_working_pieces():
    cfg = AppConfig()
    backend = build_backend(cfg)
    try:
        assert isinstance(backend, Backend)
        assert isinstance(backend.adapter, SQLAdapter)
        snapshot = backend.schema.get()
        assert snapshot.dialect == "sqlite"
        assert len(snapshot.tables) > 0
    finally:
        backend.executor.shutdown()
        backend.engine.dispose()


def test_build_backend_rejects_unimplemented_type():
    cfg = AppConfig()
    cfg.database.type = "mongodb"  # not implemented yet — must fail fast and clearly
    try:
        build_backend(cfg)
        assert False, "expected ValueError for unimplemented backend type"
    except ValueError as e:
        assert "mongodb" in str(e)


# ── QueryAgent rename + adapter injection (back-compat) ─────────────────

def test_sql_agent_is_query_agent_alias():
    assert SQLAgent is QueryAgent


def test_query_agent_without_adapter_defaults_to_sql_adapter():
    """3-arg construction (the old call shape) must behave exactly as
    before: it builds its own SQLAdapter from the given executor."""
    cfg = AppConfig()
    cfg.anthropic.api_key = "test-key"
    backend = build_backend(cfg)
    try:
        agent = QueryAgent(cfg, backend.schema, backend.executor)  # no adapter arg
        assert isinstance(agent._adapter, SQLAdapter)
    finally:
        backend.executor.shutdown()
        backend.engine.dispose()


def test_query_agent_uses_injected_adapter_when_given():
    """New call shape (4 args): the adapter the factory chose must be the
    one actually used, not silently replaced by a default."""
    cfg = AppConfig()
    cfg.anthropic.api_key = "test-key"
    backend = build_backend(cfg)
    try:
        sentinel_adapter = SQLAdapter(backend.executor)
        agent = QueryAgent(cfg, backend.schema, backend.executor, sentinel_adapter)
        assert agent._adapter is sentinel_adapter
    finally:
        backend.executor.shutdown()
        backend.engine.dispose()
