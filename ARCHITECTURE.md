# Architecture — Talk-to-DB v2

## Design stance

1. **The model proposes, the firewall disposes.** The LLM is treated as an
   untrusted SQL author. Safety properties (read-only, single statement,
   schema scope, limits) are enforced in code at the tool boundary — never by
   prompt alone. The prompt asks for good behaviour; the firewall guarantees it.
2. **Transparency over magic.** Every answer ships with the exact SQL, the
   rows, and the guardrail stamps, so a wrong answer is auditable in seconds.
3. **Two-line setup.** API key + DB URL in one YAML file; every other knob has
   a safe default.

## Request flow

```
 Browser (ui/index.html)
   │  POST /api/ask {question, history}
   ▼
 FastAPI (app/main.py)
   │  bearer auth (optional) → per-IP rate limit → pydantic validation
   ▼
 SQLAgent (app/agent/orchestrator.py)        SchemaService (cached snapshot)
   │  system prompt = rules + live schema  ◄─┘
   │
   │  ┌────────────── agent loop (≤ max_agent_turns) ──────────────┐
   │  │ Claude Messages API ── tool_use: execute_sql(sql) ─────┐   │
   │  │                                                        ▼   │
   │  │                      SQL FIREWALL (guardrails/validator)   │
   │  │                        parse AST → 1 stmt? SELECT-only?    │
   │  │                        forbidden nodes/functions? tables   │
   │  │                        in allowlist? force/clamp LIMIT     │
   │  │            rejected ──► tool_result(is_error) ─► retry     │
   │  │            valid ─────► QueryExecutor                      │
   │  │                          read-only session, timeouts,      │
   │  │                          row cap, cell truncation, audit   │
   │  │            rows (capped for model) ─► tool_result ─► loop  │
   │  └─────────── stop_reason == end_turn → final answer ─────────┘
   ▼
 AskResponse: answer + sql + columns/rows + steps + stamps (blocked/turns/ms)
   ▼
 UI renders: answer card · stamp strip · SHOW SQL · result table
```

## Module map

| Path | Responsibility |
|------|----------------|
| `app/config.py` | typed config from `config.yaml`, env override, write-mode hard-refused |
| `app/guardrails/validator.py` | the AST firewall — the security core |
| `app/guardrails/ratelimit.py` | per-IP token bucket |
| `app/db/engine.py` | engine + per-dialect read-only / timeout hardening |
| `app/db/introspect.py` | cached schema snapshot → prompt text + sidebar JSON |
| `app/db/executor.py` | guarded execution, audit log |
| `app/agent/prompts.py` | system prompt, tool definition, dialect notes |
| `app/agent/orchestrator.py` | tool-use loop, budgets, self-correction |
| `app/main.py` | wiring, auth, routes, static UI |
| `ui/index.html` | neo-brutalist frontend, escapes all DB content |

## Trust boundaries

- **User → server:** validated request models, rate limit, optional bearer token.
- **Server → LLM:** schema (+ optional sample rows — config-off for sensitive data),
  question, capped tool results. Never credentials.
- **LLM → DB:** only through the firewall + read-only session. No raw execution path exists in the codebase.
- **DB → LLM/UI:** results are untrusted: capped, truncated, marked as data for
  the model; HTML-escaped in the browser.

## Deliberate non-goals

- **Writes** — refused at config load. A write-capable agent needs human
  approval flows and is a different product.
- **Multi-tenant SaaS concerns** (SSO, per-user DB grants, Redis rate limits) —
  this is a single-process internal tool; swap-in points are noted in code.
