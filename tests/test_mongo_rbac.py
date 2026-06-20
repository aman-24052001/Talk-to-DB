"""Tests for verify_readonly_credentials — the startup RBAC probe that
turns 'use a read-only Mongo role' from a docs footnote into a logged
signal. The privilege-parsing logic is tested with a fake client whose
db.command() returns canned connectionStatus payloads (the real command
needs a live authenticated server; that path is covered by the
mongo_integration job).
"""

from app.backends.mongo.engine import verify_readonly_credentials
from app.config import AppConfig


def _cfg(url):
    cfg = AppConfig()
    cfg.database.url = url
    return cfg


class _FakeClient:
    def __init__(self, conn_status):
        self._conn_status = conn_status

    def get_default_database(self):
        client = self

        class _DB:
            def command(self, name, **kwargs):
                assert name == "connectionStatus"
                return client._conn_status
        return _DB()


def test_demo_mode_skips_real_check():
    status = verify_readonly_credentials(_FakeClient({}), _cfg("mongomock://demo"))
    assert "demo mode" in status


def test_read_only_user_passes():
    conn = {"authInfo": {
        "authenticatedUsers": [{"user": "reader", "db": "mydb"}],
        "authenticatedUserPrivileges": [
            {"resource": {"db": "mydb", "collection": ""},
             "actions": ["find", "listCollections", "listIndexes"]},
        ],
    }}
    status = verify_readonly_credentials(_FakeClient(conn), _cfg("mongodb://h/mydb"))
    assert "read-only" in status and "good" in status


def test_write_privileges_are_flagged():
    conn = {"authInfo": {
        "authenticatedUsers": [{"user": "admin", "db": "mydb"}],
        "authenticatedUserPrivileges": [
            {"resource": {"db": "mydb", "collection": ""},
             "actions": ["find", "insert", "update", "remove"]},
        ],
    }}
    status = verify_readonly_credentials(_FakeClient(conn), _cfg("mongodb://h/mydb"))
    assert "WRITE privileges" in status
    assert "insert" in status and "update" in status and "remove" in status


def test_unauthenticated_connection_is_flagged():
    conn = {"authInfo": {"authenticatedUsers": [], "authenticatedUserPrivileges": []}}
    status = verify_readonly_credentials(_FakeClient(conn), _cfg("mongodb://h/mydb"))
    assert "WITHOUT authentication" in status


def test_probe_failure_does_not_raise():
    class _Broken:
        def get_default_database(self):
            raise RuntimeError("server unreachable")

    status = verify_readonly_credentials(_Broken(), _cfg("mongodb://h/mydb"))
    assert "could not verify" in status


def test_build_mongo_backend_logs_readonly_check(caplog):
    """The demo backend should log the (benign) read-only check at INFO."""
    import logging

    from app.backends.factory import build_backend

    cfg = AppConfig()
    cfg.database.url = "mongomock://demo"
    with caplog.at_level(logging.INFO, logger="talk_to_db"):
        backend = build_backend(cfg)
        backend.close()
    assert any("read-only check" in r.message for r in caplog.records)
