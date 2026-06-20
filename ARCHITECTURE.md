# Architecture — Talk-to-DB v3

## Design stance

1. **The model proposes, the firewall disposes.** The LLM is treated as an
   untrusted query author, SQL or Mongo alike. Safety properties (read-only,
   single statement/operation, schema scope, limits) are enforced in code at
   the tool boundary — never by prompt alone. The prompt asks for good
   behaviour; the firewall guarantees it.
2. **Transparency over magic.** Every answer ships with the exact query, the
   rows, and the guardrail stamps, so a wrong answer is auditable in seconds.
3. **Two-line setup.** API key + DB URL in one YAML file; every other knob has
   a safe default.

## Request flow

The diagram below shows the SQL path concretely; the Mongo path is the
*identical* shape with `validate_sql`/`QueryExecutor` swapped for
`validate_mongo_query`/`MongoExecutor` behind the same `BackendAdapter` —
`QueryAgent` itself never branches on which backend it's talking to.

```
 Browser (ui/index.html)
   │  POST /api/ask {question, history}
   ▼
 FastAPI (app/main.py)
   │  bearer auth (optional) → per-IP rate limit → pydantic validation
   ▼
 QueryAgent (app/agent/orchestrator.py)        SchemaService (cached snapshot)
   │  system prompt = rules + live schema  ◄─┘   (SQLSchemaService or
   │                                              MongoSchemaService)
   │  ┌────────────── agent loop (≤ max_agent_turns) ──────────────┐
   │  │ Claude Messages API ── tool_use ───────────────────────┐   │
   │  │   adapter.parse_tool_input(tu.input) → raw query string │   │
   │  │                                                        ▼   │
   │  │                  adapter.validate()  (the firewall)        │
   │  │                    SQL: AST parse → 1 stmt? SELECT-only?   │
   │  │                         forbidden nodes/functions? tables  │
   │  │                         in allowlist? force/clamp LIMIT    │
   │  │                    Mongo: stage allowlist, recursive       │
   │  │                         $where/$function scan, collection  │
   │  │                         allowlist, force/clamp $limit      │
   │  │            rejected ──► tool_result(is_error) ─► retry     │
   │  │            valid ─────► adapter.execute()                  │
   │  │                          read-only connection, timeouts,   │
   │  │                          row cap, cell truncation, audit   │
   │  │            rows (capped for model) ─► tool_result ─► loop  │
   │  └─────────── stop_reason == end_turn → final answer ─────────┘
   ▼
 AskResponse: answer + sql + columns/rows + steps + stamps (blocked/turns/ms)
   ▼
 UI renders: answer card · stamp strip · SHOW SQL · result table
```

`AskResponse.sql` (and `StepOut.sql`) hold the literal SQL text for the SQL
backend, or a canonical JSON string (`{"operation","collection","query"}`)
for Mongo — the field name is deliberately not yet renamed to something
generic; see `app/backends/base.py`'s note on why that rename is deferred.

## Module map

| Path | Responsibility |
|------|----------------|
| `app/config.py` | typed config from `config.yaml`, env override, write-mode hard-refused, backend-type inference from `database.url`'s scheme |
| `app/backends/base.py` | the `BackendAdapter` Protocol — what `QueryAgent` needs from any backend |
| `app/backends/factory.py` | picks SQL or Mongo from `cfg.resolved_database_type`, returns a wired `Backend` (engine/client, schema service, executor, adapter, shutdown hook) |
| `app/backends/sql.py` | SQL adapter — composes the modules below behind the contract |
| `app/guardrails/validator.py` | the SQL AST firewall — security core for the SQL path |
| `app/guardrails/ratelimit.py` | per-IP token bucket |
| `app/db/engine.py` | SQL engine + per-dialect read-only / timeout hardening |
| `app/db/introspect.py` | cached SQL schema snapshot → prompt text + sidebar JSON |
| `app/db/executor.py` | guarded SQL execution, audit log |
| `app/backends/mongo/adapter.py` | Mongo adapter — composes the four files below behind the contract |
| `app/backends/mongo/engine.py` | Mongo connection + capability-narrowed read-only wrapper (no write methods reachable, not just permission-checked) |
| `app/backends/mongo/introspect.py` | sampling-based schema inference (Mongo is schemaless — field name/type/presence% inferred from sampled docs) |
| `app/backends/mongo/validator.py` | the Mongo firewall — stage allowlist + recursive `$where`/`$function`/`$accumulator` scan, security core for the Mongo path |
| `app/backends/mongo/executor.py` | guarded Mongo execution, audit log |
| `app/agent/prompts.py` | SQL system prompt, tool definition, dialect notes |
| `app/backends/mongo/prompts.py` | Mongo system prompt, tool definition |
| `app/agent/orchestrator.py` | `QueryAgent` — tool-use loop, budgets, self-correction; backend-agnostic, talks only to `BackendAdapter` |
| `app/main.py` | wiring via `build_backend(cfg)`, auth, routes, static UI |
| `ui/index.html` | neo-brutalist frontend, escapes all DB content |

`POST /api/ask` and `POST /api/ask/stream` share the same `QueryAgent` loop.
The stream variant (`agent.ask_stream`) yields the same loop's internal
events (`thinking` / `sql` / `blocked` / `error` / `done`) as Server-Sent
Events instead of returning one blocking JSON payload, so the UI can render
agent progress (firewall blocks, retries, query attempts) as it happens —
identically whether the backend behind it is SQL or Mongo.

## Trust boundaries

- **User → server:** validated request models, rate limit, optional bearer token.
- **Server → LLM:** schema (+ optional sample rows — config-off for sensitive data),
  question, capped tool results. Never credentials.
- **LLM → DB:** only through the firewall + read-only connection (a session
  flag for SQL; a capability-narrowed wrapper object with no write methods
  for Mongo). No raw execution path exists in the codebase for either.
- **DB → LLM/UI:** results are untrusted: capped, truncated, marked as data for
  the model; HTML-escaped in the browser.

## Deliberate non-goals

- **Writes** — refused at config load. A write-capable agent needs human
  approval flows and is a different product.
- **Multi-tenant SaaS concerns** (SSO, per-user DB grants, Redis rate limits) —
  this is a single-process internal tool; swap-in points are noted in code.
- **Multiple simultaneous backends in one deployment** — one connected
  datastore per deployment today, chosen by `database.url`. Answering one
  question across an SQL source *and* a Mongo source at once would need a
  Planner/Synthesizer layer on top of `QueryAgent`, which doesn't exist yet.
