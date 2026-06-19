"""Hardening tests added in the final pass: cross-dialect escape attempts,
the rate limiter, credential masking, and demo-data sanity."""
import sqlite3
from pathlib import Path

import pytest

from app.db.executor import _clean_db_error
from app.guardrails.ratelimit import RateLimiter
from app.guardrails.validator import SQLRejected, validate_sql

DEMO = Path(__file__).resolve().parent.parent / "data" / "demo.db"


# ------------------------------------------------------- dialect escapes
def test_pg_sleep_blocked_on_postgres():
    with pytest.raises(SQLRejected, match="blocked by policy"):
        validate_sql("SELECT pg_sleep(30)", dialect="postgres",
                     known_tables=set(), max_rows=10)


def test_mysql_into_outfile_blocked():
    with pytest.raises(SQLRejected):
        validate_sql("SELECT * FROM t_shirts INTO OUTFILE '/tmp/x'",
                     dialect="mysql", known_tables={"t_shirts"}, max_rows=10)


def test_union_exfil_of_unknown_table_blocked():
    with pytest.raises(SQLRejected, match="not in the allowed schema"):
        validate_sql("SELECT name FROM customers UNION SELECT sql FROM sqlite_master",
                     dialect="sqlite", known_tables={"customers"}, max_rows=10)


def test_set_operation_gets_limit():
    out = validate_sql("SELECT 1 UNION SELECT 2", dialect="sqlite",
                       known_tables=set(), max_rows=7)
    assert "limit 7" in out.sql.lower()


# ------------------------------------------------------- supporting layers
def test_rate_limiter_blocks_then_refills():
    rl = RateLimiter(per_minute=60)  # 1 token/sec refill, burst of 60
    assert all(rl.allow("ip") for _ in range(60))
    assert rl.allow("ip") is False           # bucket drained
    assert rl.allow("other-ip") is True      # per-key isolation


def test_db_error_masks_credentials():
    msg = _clean_db_error(Exception(
        "connection failed for mysql+pymysql://aman:S3cret!@10.0.0.5:3306/prod"))
    assert "S3cret!" not in msg
    assert "aman:***@" in msg


def test_demo_db_is_seeded_and_consistent():
    assert DEMO.exists(), "run scripts/create_demo_db.py"
    conn = sqlite3.connect(DEMO)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"t_shirts", "discounts", "customers", "orders", "order_items"} <= tables
    orphan = conn.execute(
        "SELECT COUNT(*) FROM order_items oi LEFT JOIN orders o USING(order_id) "
        "WHERE o.order_id IS NULL").fetchone()[0]
    assert orphan == 0
    conn.close()
