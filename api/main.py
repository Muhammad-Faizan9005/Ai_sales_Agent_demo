"""FastAPI app for the AI sales agent.

    uvicorn api.main:app --port 8002 --reload

Port 8002 is the project default and lives in config (API_PORT), so the
widget, CORS and the run command cannot drift apart.

Endpoints:
    POST /api/chat         SSE stream of one turn
    POST /api/end          conversation finished (widget unload)
    POST /api/cal-callback n8n reports a booking outcome (X-Webhook-Secret)
    GET  /api/health       readiness + config visibility
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from api import chat as chat_module
from api import llm, n8n_client, retrieval, tools
from api.config import get_settings
from api.dashboard import router as dashboard_router
from api.kb import allowed_paths, system_prompt
from api.schemas import CalCallback, ChatRequest, HealthResponse
from api.store import SESSIONS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("api")

SETTINGS = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Load the embedding model and index up front. Off the event loop because
    # it is ~2s of blocking CPU; the first visitor should not pay for it.
    # On a machine with no kb/index/ yet this also builds it from kb/raw/, so
    # startup can take ~a minute once (see kb/autobuild.py).
    ready = await asyncio.to_thread(retrieval.warm)
    status = await llm.probe()
    log.info(
        "model=%s reachable=%s | prompt ~%dk tokens | %d paths | retrieval %s (%d chunks)",
        status["model"],
        status["reachable"],
        len(system_prompt()) // 4000,
        len(allowed_paths()),
        "ready" if ready else "UNAVAILABLE",
        retrieval.chunk_count(),
    )
    if not status["reachable"]:
        log.warning("Ollama unreachable at %s -- chat will degrade", SETTINGS.ollama_base_url)
    if not ready:
        # The build is automatic, so getting here means it could not run: an
        # empty kb/raw/ (needs python kb/kb_build.py, which fetches the live
        # site), KB_AUTOBUILD=false, or a build error logged above.
        log.warning(
            "No FAISS index and could not build one -- run: python kb/embed.py"
        )
    async def reap_idle_sessions() -> None:
        idle_seconds = max(1, SETTINGS.session_idle_minutes) * 60
        while True:
            await asyncio.sleep(min(60, idle_seconds))
            count = await chat_module.finalise_idle_sessions(idle_seconds)
            if count:
                log.info("finalised %d idle sales-agent sessions", count)

    reaper = asyncio.create_task(reap_idle_sessions())
    yield
    reaper.cancel()
    try:
        await reaper
    except asyncio.CancelledError:
        pass
    # Let queued n8n emits land rather than dropping them on restart.
    await n8n_client.flush()


app = FastAPI(title="AI Sales Agent", version="1.0.0", lifespan=lifespan)

# The widget is injected into pages served from another origin, so it needs
# explicit CORS. Origins come from config, never "*", because the endpoint
# costs money to call.
app.add_middleware(
    CORSMiddleware,
    allow_origins=SETTINGS.origins,
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)

# Served from this same origin, so the browser makes no preflight and
# X-Dashboard-Token needs no CORS allowance. Every data route under it is gated
# on that header; only the empty HTML shell is public.
app.include_router(dashboard_router)


@app.post("/api/chat")
async def post_chat(request: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        chat_module.run_turn(
            request.message, request.session_id, request.page_path, request.debug
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Without this nginx buffers the stream and the visitor sees the
            # whole reply at once, which defeats streaming entirely.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/end")
async def post_end(payload: dict) -> dict:
    session_id = str(payload.get("session_id") or "").strip()
    if session_id:
        await chat_module.end_conversation(session_id)
    return {"ok": True}


@app.post("/api/cal-callback")
async def post_cal_callback(
    payload: CalCallback,
    x_webhook_secret: str | None = Header(default=None),
) -> dict:
    """n8n reports what Cal.com actually did. The only source of booking truth.

    The booking turn is parked on an asyncio.Event waiting for this. Applying
    the outcome here rather than in the tool keeps a single writer, so a late or
    retried callback cannot race the turn that fired it.

    Deliberately does NOT take session.turn_lock: the waiting turn holds it, and
    acquiring it here would deadlock the exact request that releases it. Safe
    without: this and the turn share one event loop, and the turn is parked at an
    await while this runs.
    """
    expected = SETTINGS.n8n_webhook_secret
    if not expected:
        # Refuse rather than default open -- this route mutates booking state.
        log.warning("cal callback refused: N8N_WEBHOOK_SECRET is unset")
        raise HTTPException(503, "callback not configured")
    if not secrets.compare_digest(x_webhook_secret or "", expected):
        raise HTTPException(401, "bad webhook secret")

    data = payload.model_dump()
    session = SESSIONS.get(payload.session_id)
    n8n_client.record_ack(payload.session_id, data)
    applied = tools.apply_booking_ack(session, data)
    waiting = n8n_client.resolve_ack(payload.session_id)
    log.info(
        "cal ack %s %s ok=%s uid=%s applied=%s waiting=%s",
        payload.session_id,
        payload.action,
        payload.ok,
        payload.booking_uid,
        applied,
        waiting,
    )
    return {"ok": True, "applied": applied, "waiting": waiting}


@app.get("/console")
async def get_console() -> FileResponse:
    """Test console: the widget's stream plus its telemetry, side by side.

    Unauthenticated, unlike the dashboard -- it reads no stored data and only
    reflects the session you are typing into. It is a local test rig; bind to
    localhost and do not deploy it. The `debug` SSE frames it relies on are
    opt-in per request, so a visitor on the widget never receives them.
    """
    page = SETTINGS.console_dir / "index.html"
    if not page.exists():
        raise HTTPException(404, "console/index.html missing")
    return FileResponse(page)


@app.get("/api/health", response_model=HealthResponse)
async def get_health() -> HealthResponse:
    status = await llm.probe()
    indexed = retrieval.chunk_count()
    return HealthResponse(
        # Retrieval down is degraded, not ok: the agent can still talk but
        # cannot ground anything it says about services.
        status="ok" if status["reachable"] and indexed else "degraded",
        model=status["model"],
        kb_tokens=len(system_prompt()) // 4,
        allowlist_paths=len(allowed_paths()),
        active_sessions=SESSIONS.count(),
        retrieval_ready=bool(indexed),
        indexed_chunks=indexed,
        embed_model=SETTINGS.embed_model,
    )
