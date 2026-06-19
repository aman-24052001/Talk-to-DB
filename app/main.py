"""FastAPI application — wires config → engine → schema → agent, serves UI.

B1 fix: executor.shutdown() called in lifespan teardown.
New:    /api/ask/stream SSE endpoint for real-time agent progress.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.backends.factory import build_backend
from app.config import PROJECT_ROOT, __version__, get_config
from app.guardrails.ratelimit import RateLimiter
from app.schemas import AskRequest, AskResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("talk_to_db")

UI_DIR = PROJECT_ROOT / "ui"


def _ensure_demo_db(cfg) -> None:
    demo = (PROJECT_ROOT / "data" / "demo.db").resolve()
    if cfg.database.url == f"sqlite:///{demo}" and not demo.exists():
        import runpy
        log.info("demo database missing — seeding %s", demo)
        runpy.run_path(str(PROJECT_ROOT / "scripts" / "create_demo_db.py"), run_name="__main__")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    _ensure_demo_db(cfg)
    backend = build_backend(cfg)
    snapshot = backend.schema.get()
    log.info("connected to %s — %d table(s) visible", snapshot.dialect, len(snapshot.tables))

    app.state.cfg = cfg
    app.state.engine = backend.engine
    app.state.schema = backend.schema
    app.state.executor = backend.executor
    app.state.adapter = backend.adapter
    app.state.limiter = RateLimiter(cfg.server.rate_limit_per_minute)
    app.state.agent = None
    yield
    # B1 FIX: clean shutdown of thread pool
    app.state.executor.shutdown()
    backend.engine.dispose()


app = FastAPI(
    title="Talk-to-DB",
    version=__version__,
    lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,
)


# ── auth ───────────────────────────────────────────────────────────────────────
def require_auth(request: Request) -> None:
    token = request.app.state.cfg.server.auth_token
    if not token:
        return
    if request.headers.get("authorization", "") != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Missing or invalid bearer token.")


def _agent(request: Request):
    if request.app.state.agent is None:
        from app.agent.orchestrator import QueryAgent
        request.app.state.agent = QueryAgent(
            request.app.state.cfg,
            request.app.state.schema,
            request.app.state.executor,
            request.app.state.adapter,
        )
    return request.app.state.agent


def _check_rate(request: Request) -> str:
    ip = request.client.host if request.client else "unknown"
    if not request.app.state.limiter.allow(ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Slow down a little.")
    return ip


# ── routes ─────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health(request: Request):
    cfg = request.app.state.cfg
    return {
        "status": "ok",
        "version": __version__,
        "dialect": request.app.state.engine.url.get_backend_name(),
        "model": cfg.anthropic.model,
        "read_only": True,
        "api_key_present": bool(cfg.resolved_api_key),
    }


@app.get("/api/schema", dependencies=[Depends(require_auth)])
def get_schema(request: Request, refresh: bool = False):
    return request.app.state.schema.get(force=refresh).to_api()


@app.post("/api/ask", response_model=AskResponse, dependencies=[Depends(require_auth)])
def ask(request: Request, body: AskRequest):
    """Blocking endpoint — returns complete response (backward compat)."""
    _check_rate(request)
    try:
        agent = _agent(request)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    try:
        result = agent.ask(body.question, [t.model_dump() for t in body.history])
    except Exception as e:
        log.exception("ask failed")
        raise HTTPException(status_code=502, detail=f"Agent error: {e}") from e
    return AskResponse(
        answer=result.answer, sql=result.sql, columns=result.columns,
        rows=result.rows, row_count=result.row_count, truncated=result.truncated,
        steps=[asdict(s) for s in result.steps], turns=result.turns,
        blocked=result.blocked, elapsed_ms=result.elapsed_ms, model=result.model,
    )


@app.post("/api/ask/stream", dependencies=[Depends(require_auth)])
def ask_stream(request: Request, body: AskRequest):
    """SSE streaming endpoint — yields events as the agent works.

    Events: thinking | sql | blocked | error | done | err
    """
    _check_rate(request)
    try:
        agent = _agent(request)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    def generate():
        try:
            yield from agent.ask_stream(
                body.question, [t.model_dump() for t in body.history]
            )
        except Exception as e:
            log.exception("ask_stream failed")
            import json
            yield f"event: err\ndata: {json.dumps({'detail': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse(status_code=500, content={"detail": "Internal error. Check server logs."})


# ── static UI ──────────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(UI_DIR / "index.html")


if (UI_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=UI_DIR / "assets"), name="assets")
