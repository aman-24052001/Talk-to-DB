# Talk-to-DB v3

[![CI](https://github.com/aman-24052001/Talk-to-DB/actions/workflows/ci.yml/badge.svg)](https://github.com/aman-24052001/Talk-to-DB/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](.github/workflows/ci.yml)

Ask your database questions in plain English. A Claude-powered agent writes
the query, a firewall validates it, a read-only connection executes it, and
the UI shows you the answer **plus the exact query and rows behind it** —
nothing is hidden, nothing can be written. Works against SQL (SQLite,
PostgreSQL, MySQL) or MongoDB — picked automatically from `database.url`.

**[Live demo →](https://aman-24052001.github.io/Talk-to-DB/)**

> v2 was a ground-up rewrite of the 2023 GooglePalm/LangChain prototype.
> v3 adds real-time SSE streaming, a mobile-friendly UI, and bug fixes.
> See [`UPGRADE_NOTES.md`](UPGRADE_NOTES.md) for the full audit of what was
> broken in the original and why each piece was replaced.

---

## Quickstart (60 seconds)

```bash
pip install -r requirements.txt
```

Open **`config.yaml`** — the only file you touch — and set two things:

```yaml
anthropic:
  api_key: "sk-ant-..."          # or:  export ANTHROPIC_API_KEY=sk-ant-...

database:
  url: "sqlite:///data/demo.db"  # ships with a seeded demo DB — works as-is
```

Run it:

```bash
python run.py
# →  http://127.0.0.1:7860
```

A demo SQLite database (t-shirt store: products, discounts, customers,
orders, order items) is included so the app works before you connect your own
DB. Regenerate it any time with `python scripts/create_demo_db.py`.

### Connecting your own database

**SQL** — any SQLAlchemy URL works; install the matching driver (commented
in `requirements.txt`):

| Database   | URL example                                          | Driver            |
|------------|------------------------------------------------------|-------------------|
| SQLite     | `sqlite:///path/to.db`                               | built-in          |
| PostgreSQL | `postgresql+psycopg2://user:pass@host:5432/db`       | `psycopg2-binary` |
| MySQL      | `mysql+pymysql://user:pass@host:3306/db`             | `pymysql`         |

**MongoDB** — detected automatically from a `mongodb://` or `mongodb+srv://`
URL; the database name must be in the URL path (e.g.
`mongodb://host:27017/mydb`), since unlike SQL there's no single "current
database" to fall back to:

| Database | URL example                                  | Driver    |
|----------|-----------------------------------------------|-----------|
| MongoDB  | `mongodb://user:pass@host:27017/mydb`        | `pymongo` (installed by default) |

MongoDB has no session-level read-only flag the way SQLite/Postgres/MySQL
do, so enforcement there is two-layer instead: a `read`-only RBAC role
(recommended, see below) plus capability narrowing in code — the connection
object literally never exposes `insert`/`update`/`delete`/`drop` methods,
not just permission-checks them.

**Strongly recommended either way:** create a dedicated DB user with
read-only grants (`SELECT` for SQL, the built-in `read` role for Mongo) and
use that in the URL. The app enforces read-only on its own anyway, but
least-privilege credentials make it belt-and-suspenders.

---

## Security & guardrails (defence in depth)

| # | Layer | What it stops |
|---|-------|---------------|
| 1 | **Query firewall** — SQL: AST walk via `sqlglot` (single statement, SELECT-only roots, forbidden-node walk, function blocklist, table allowlist, forced `LIMIT`). Mongo: structural validation (operation must be `find`/`aggregate`, pipeline stages checked against an explicit allowlist, recursive scan for `$where`/`$function`/`$accumulator` at any nesting depth, collection allowlist, forced `$limit`) | `DROP`/`INSERT`/`UPDATE`/`DELETE`, multi-statement chains, `PRAGMA`/`ATTACH`/`SET`, `SELECT INTO`, `load_extension`/`pg_sleep`-class escapes; Mongo's `$out`/`$merge`/`$function`/`$where` equivalents; querying hidden tables/collections; runaway result sets |
| 2 | **Read-only connection** — SQL: `PRAGMA query_only` (SQLite), `default_transaction_read_only` (Postgres), `SESSION TRANSACTION READ ONLY` (MySQL). Mongo: capability-narrowed wrapper — the connection object has no write methods to call, not just permission checks | any write that somehow got past layer 1 |
| 3 | **Execution guards** — server-side `statement_timeout` + wall-clock timeout, row cap with truncation flag, 400-char cell cap | long-running queries, memory blowups, huge blobs entering the LLM context |
| 4 | **Agent budgets** — max turns, max 3 consecutive firewall blocks, capped tool-result size, capped history | infinite self-correction loops, token-burn, context flooding |
| 5 | **Prompt-injection posture** — query results are wrapped as untrusted data and the model is instructed to ignore instructions inside them; since the only tool is firewalled read-only SQL, the blast radius of a poisoned row is a misleading sentence, not an action | malicious strings stored in your tables |
| 6 | **Server hygiene** — binds `127.0.0.1` by default, optional bearer token, per-IP rate limit, no docs endpoints, sanitized error messages | accidental network exposure, brute-force, credential leaks in stack traces |
| 7 | **Audit log** — every generated, blocked, executed, and failed statement appended to `data/audit.log` as JSON lines | "what did the agent actually run?" ever being unanswerable |
| 8 | **XSS-safe UI** — every DB cell rendered escaped, never as HTML | stored-XSS via table contents |

Writes are a deliberate **non-goal**: setting `read_only: false` in config is
rejected at startup. An LLM with write access to a database is a different
risk class of product.

`sample_rows_in_schema` sends a few real cell/field values to the Anthropic
API to improve query accuracy. Set it to `0` for sensitive databases.

---

## Configuration reference

Everything optional has safe defaults — see `config.yaml` for the annotated
full list: row caps, timeouts, turn budgets, table allow/deny lists, host/port,
bearer token, rate limit, audit path.

## API

| Route | Method | Purpose |
|-------|--------|---------|
| `/` | GET | the UI |
| `/api/health` | GET | status, dialect, model, key presence |
| `/api/schema` | GET | introspected schema (`?refresh=1` busts cache) |
| `/api/ask` | POST | `{question, history[]}` → answer + SQL + rows + guardrail stamps (blocking) |
| `/api/ask/stream` | POST | same input, **Server-Sent Events** response — emits `thinking` / `sql` / `blocked` / `error` / `done` / `err` events as the agent works, so the UI can show live progress instead of waiting on one big response |

## Tests

```bash
python -m pytest tests/ -q     # 81 tests: SQL firewall + Mongo firewall, RO
                                # enforcement for both, schema introspection,
                                # SSE streaming, full E2E flow for both backends
```

## Docker

```bash
docker build -t talk-to-db .
docker run -p 7860:7860 -e ANTHROPIC_API_KEY=sk-ant-... talk-to-db
```

## Project layout

```
config.yaml              ← the only file you edit
run.py                   ← entrypoint
app/
  config.py              typed config, env override, backend-type inference
  main.py                FastAPI wiring, auth, rate limit
  agent/                 Claude tool-use loop + SQL prompts/tool schema
  backends/              BackendAdapter contract + factory (picks SQL or Mongo)
    sql.py               SQL adapter (composes guardrails/ + db/ below)
    mongo/                Mongo adapter: engine (RO wrapper), introspect
                          (sampling), validator (pipeline firewall), executor
  guardrails/            AST SQL firewall + rate limiter
  db/                    read-only SQL engine, introspection, guarded executor
ui/index.html            neo-brutalist single-file frontend
scripts/create_demo_db.py
tests/                   guardrail + E2E suite
```
