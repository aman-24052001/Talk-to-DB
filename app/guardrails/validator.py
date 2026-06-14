"""SQL firewall.

Every statement the LLM produces passes through here BEFORE touching the
database. Validation is done on the parsed AST (sqlglot), never with regex
or string matching, so comments / casing / clever whitespace can't sneak
anything past it.

Policy: exactly one statement, it must be a pure read (SELECT / UNION /
CTE-of-SELECTs), it may only touch introspected+allowlisted tables, it may
not call escape-hatch functions, and a row LIMIT is force-injected/clamped.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

# --- statement classes that are never allowed anywhere in the tree -------
_FORBIDDEN_NAMES = [
    "Insert", "Update", "Delete", "Merge", "Drop", "Create", "Alter",
    "TruncateTable", "Grant", "Revoke", "Command", "Transaction", "Commit",
    "Rollback", "Set", "Pragma", "Into", "Use", "Attach", "Detach", "Copy",
    "LoadData", "Lock", "Kill", "Analyze", "Install", "Describe",
]
FORBIDDEN_NODES: tuple[type, ...] = tuple(
    c for n in _FORBIDDEN_NAMES
    if isinstance(c := getattr(exp, n, None), type) and issubclass(c, exp.Expression)
)

# --- read-only roots ------------------------------------------------------
_ALLOWED_ROOT_NAMES = ["Select", "Union", "Intersect", "Except", "Subquery"]
ALLOWED_ROOTS: tuple[type, ...] = tuple(
    c for n in _ALLOWED_ROOT_NAMES
    if isinstance(c := getattr(exp, n, None), type)
)

# --- functions that read files / sleep / execute / escalate --------------
FORBIDDEN_FUNCTIONS = {
    # sqlite
    "load_extension", "readfile", "writefile", "fts3_tokenizer", "edit",
    # postgres
    "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_read_file",
    "pg_read_binary_file", "pg_ls_dir", "lo_import", "lo_export",
    "dblink", "dblink_exec", "pg_terminate_backend", "pg_cancel_backend",
    "copy_from", "pg_reload_conf", "set_config", "query_to_xml",
    # mysql
    "load_file", "sleep", "benchmark", "sys_exec", "sys_eval", "sys_set",
    # generic
    "xp_cmdshell", "openrowset", "opendatasource",
}

_DIALECT_MAP = {
    "sqlite": "sqlite", "postgresql": "postgres", "postgres": "postgres",
    "mysql": "mysql", "mariadb": "mysql", "mssql": "tsql", "oracle": "oracle",
    "snowflake": "snowflake", "duckdb": "duckdb",
}


def sqlglot_dialect(sqlalchemy_backend: str) -> str:
    return _DIALECT_MAP.get(sqlalchemy_backend.lower(), "ansi")


class SQLRejected(Exception):
    """Raised when a statement fails policy. The message is fed back to the
    model so it can self-correct, so keep it specific and actionable."""


@dataclass
class ValidatedSQL:
    sql: str                       # rewritten, LIMIT-enforced statement
    tables: list[str] = field(default_factory=list)
    limit_applied: int | None = None


def validate_sql(
    raw_sql: str,
    *,
    dialect: str,
    known_tables: set[str],
    max_rows: int,
) -> ValidatedSQL:
    """Parse, police and rewrite a statement. Raises SQLRejected on any violation."""
    if not raw_sql or not raw_sql.strip():
        raise SQLRejected("Empty SQL statement.")

    try:
        statements = sqlglot.parse(raw_sql, read=dialect)
    except sqlglot.errors.ParseError as e:
        raise SQLRejected(f"SQL failed to parse: {e}") from e

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SQLRejected(
            f"Exactly one statement is allowed, got {len(statements)}. "
            "Do not chain statements with ';'."
        )
    root = statements[0]

    # 1. Root must be a read.
    if not isinstance(root, ALLOWED_ROOTS):
        raise SQLRejected(
            f"Only SELECT queries are allowed. Got: {type(root).__name__}. "
            "This connection is read-only — INSERT/UPDATE/DELETE/DDL are blocked."
        )

    # 2. No forbidden node anywhere in the tree (catches SELECT ... INTO,
    #    sub-statement tricks, PRAGMA-in-CTE, etc.).
    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SQLRejected(
                f"Forbidden operation in query: {type(node).__name__}. "
                "Only pure read-only SELECTs are permitted."
            )

    # 3. No escape-hatch functions.
    for fn in root.find_all(exp.Func):
        name = (fn.sql_name() or "").lower()
        anon = fn.name.lower() if isinstance(fn, exp.Anonymous) else ""
        if name in FORBIDDEN_FUNCTIONS or anon in FORBIDDEN_FUNCTIONS:
            raise SQLRejected(f"Function '{name or anon}' is blocked by policy.")

    # 4. Table allowlist (CTE names are exempt — they aren't real tables).
    cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
    referenced: list[str] = []
    for t in root.find_all(exp.Table):
        name = t.name.lower()
        if not name or name in cte_names:
            continue
        referenced.append(name)
        if known_tables and name not in known_tables:
            raise SQLRejected(
                f"Table '{name}' is not in the allowed schema. "
                f"Available tables: {', '.join(sorted(known_tables))}."
            )

    # 5. Force / clamp LIMIT so a runaway query can't flood anything.
    limit_applied = None
    limit_node = root.args.get("limit")
    if isinstance(limit_node, exp.Limit):
        lit = limit_node.expression
        try:
            current = int(lit.this) if isinstance(lit, exp.Literal) else None
        except (TypeError, ValueError):
            current = None
        if current is None or current > max_rows:
            root.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
            limit_applied = max_rows
    else:
        root = root.limit(max_rows)
        limit_applied = max_rows

    return ValidatedSQL(
        sql=root.sql(dialect=dialect),
        tables=sorted(set(referenced)),
        limit_applied=limit_applied,
    )
