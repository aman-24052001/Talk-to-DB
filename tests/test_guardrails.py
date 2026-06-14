"""Guardrail tests: every known way an LLM (or a prompt-injected LLM) could
try to mutate or escape the database must be blocked at the AST firewall,
and the read-only session must hold even if the firewall were bypassed."""
import sqlite3

import pytest

from app.guardrails.validator import SQLRejected, validate_sql

TABLES = {"t_shirts", "discounts", "customers", "orders", "order_items"}


def v(sql: str, max_rows: int = 200):
    return validate_sql(sql, dialect="sqlite", known_tables=TABLES, max_rows=max_rows)


# ---------------------------------------------------------------- allowed
@pytest.mark.parametrize("sql", [
    "SELECT brand, SUM(stock_quantity) FROM t_shirts GROUP BY brand",
    "SELECT * FROM t_shirts WHERE price < 500 ORDER BY price DESC",
    """WITH rev AS (SELECT t_shirt_id, SUM(quantity*unit_price) r
       FROM order_items GROUP BY t_shirt_id)
       SELECT t.brand, SUM(rev.r) FROM rev JOIN t_shirts t USING (t_shirt_id) GROUP BY t.brand""",
    "SELECT city FROM customers UNION SELECT 'Bengaluru'",
    "SELECT (SELECT COUNT(*) FROM orders) AS n, COUNT(*) FROM customers",
])
def test_valid_reads_pass(sql):
    out = v(sql)
    assert out.sql.lower().startswith(("select", "with"))


# ---------------------------------------------------------------- writes/DDL
@pytest.mark.parametrize("sql", [
    "INSERT INTO t_shirts (brand,color,size,price,stock_quantity) VALUES ('X','Y','Z',1,1)",
    "UPDATE t_shirts SET price = 0",
    "DELETE FROM orders",
    "DROP TABLE t_shirts",
    "CREATE TABLE evil (x int)",
    "ALTER TABLE t_shirts ADD COLUMN hacked int",
    "SELECT * INTO stolen FROM customers",
])
def test_writes_and_ddl_blocked(sql):
    with pytest.raises(SQLRejected):
        v(sql)


# ---------------------------------------------------------------- tricks
def test_multi_statement_blocked():
    with pytest.raises(SQLRejected, match="one statement"):
        v("SELECT * FROM t_shirts; DROP TABLE t_shirts")


def test_comment_hidden_write_blocked():
    with pytest.raises(SQLRejected):
        v("SELECT 1; -- harmless\nDELETE FROM orders")


@pytest.mark.parametrize("sql", [
    "PRAGMA table_info(t_shirts)",
    "ATTACH DATABASE '/tmp/x.db' AS x",
])
def test_engine_escapes_blocked(sql):
    with pytest.raises(SQLRejected):
        v(sql)


def test_forbidden_function_blocked():
    with pytest.raises(SQLRejected, match="blocked by policy"):
        v("SELECT load_extension('evil')")


def test_unknown_table_blocked():
    with pytest.raises(SQLRejected, match="not in the allowed schema"):
        v("SELECT * FROM sqlite_master")


def test_cte_alias_is_not_flagged_as_unknown_table():
    v("WITH mine AS (SELECT 1 a) SELECT a FROM mine")


# ---------------------------------------------------------------- limits
def test_limit_injected_when_missing():
    out = v("SELECT * FROM orders", max_rows=50)
    assert "limit 50" in out.sql.lower()
    assert out.limit_applied == 50


def test_oversized_limit_clamped():
    out = v("SELECT * FROM orders LIMIT 999999", max_rows=100)
    assert "limit 100" in out.sql.lower()


def test_small_user_limit_kept():
    out = v("SELECT * FROM orders LIMIT 5", max_rows=100)
    assert "limit 5" in out.sql.lower()
    assert out.limit_applied is None


# ------------------------------------------------- layer 2: session is RO
def test_sqlite_session_is_physically_read_only(tmp_path):
    """Even if SQL slipped past the firewall, PRAGMA query_only stops it."""
    from sqlalchemy import text
    from app.config import AppConfig
    from app.db.engine import build_engine

    db = tmp_path / "ro.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE t (x int)")
    raw.execute("INSERT INTO t VALUES (1)")
    raw.commit()
    raw.close()

    cfg = AppConfig()
    cfg.database.url = f"sqlite:///{db}"
    eng = build_engine(cfg)
    with eng.connect() as conn:
        assert conn.execute(text("SELECT x FROM t")).scalar() == 1
        with pytest.raises(Exception, match="readonly|query_only|attempt to write"):
            conn.execute(text("INSERT INTO t VALUES (2)"))
    eng.dispose()
