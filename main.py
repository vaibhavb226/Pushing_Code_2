"""
Sysco RFP AIQ — FastAPI backend entry point.

Start:
    uvicorn main:app --reload --port 3001
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from config import get_settings
from dependencies import UPLOADS_DIR
from middleware.auth import APIKeyMiddleware
from middleware.request_id import RequestIDMiddleware, get_request_id

log = structlog.get_logger()
_startup_time = datetime.now(timezone.utc)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    UPLOADS_DIR.mkdir(exist_ok=True)
    
    from time import time
    t0 = time()

    # ── Verify email SMTP config (non-fatal) ──────────────────
    # Run as a background task instead of awaiting — don't block app startup
    async def verify_email_config():
        try:
            from services.email.factory import build_email_service
            email_svc = build_email_service(settings)
            ok = await email_svc.verify_config()
            status = "✅ Ready" if ok else "⚠️  Not configured"
            print(f"Email ({settings.email_provider.upper()}): {status}")
        except Exception as exc:
            print(f"Email config check failed: {exc}")
    
    asyncio.create_task(verify_email_config())
    print(f"⏱️  Email verification: {time() - t0:.2f}s (queued as background task)")

    # ── LLM config display ────────────────────────────────────
    t_llm = time()
    provider = settings.llm_provider.lower()
    
    if provider == "anthropic":
        api_key = settings.anthropic_api_key.get_secret_value()
        llm_status = "✅ Configured" if api_key else "⚠️  Not set"
        model_info = f"{settings.anthropic_model}"
    elif provider == "openai":
        api_key = settings.openai_api_key.get_secret_value()
        llm_status = "✅ Configured" if api_key else "⚠️  Not set"
        model_info = f"{settings.openai_model}"
    elif provider == "vertexai":
        project = settings.gcp_project
        llm_status = "✅ Configured" if project else "⚠️  Not set (need GCP_PROJECT)"
        model_info = f"{settings.vertex_model} (IAM-based auth)"
    else:
        llm_status = "⚠️  Unknown provider"
        model_info = ""
    
    print(f"LLM Provider:   {settings.llm_provider} / {model_info}")
    print(f"LLM Status:     {llm_status}")
    print(f"⏱️  LLM config: {time() - t_llm:.2f}s")

    # ── Startup backfills (non-blocking background tasks) ─────
    t_backfill = time()
    try:
        from utils.backfill import (
            backfill_email_attachments,
            backfill_line_items_from_files,
            backfill_solicited_suppliers,
            backfill_uploaded_files,
        )
        asyncio.create_task(backfill_uploaded_files())
        asyncio.create_task(backfill_solicited_suppliers())
        asyncio.create_task(backfill_email_attachments())
        asyncio.create_task(backfill_line_items_from_files())
        print(f"⏱️  Backfill tasks: {time() - t_backfill:.2f}s (queued)")
    except ImportError:
        pass  # Backfill module created in Phase 5

    # ── Demo data injection ───────────────────────────────────
    t_demo = time()
    try:
        from services.demo_data import ensure_bounce_demo
        asyncio.create_task(ensure_bounce_demo())
        print(f"⏱️  Demo data: {time() - t_demo:.2f}s (queued)")
    except ImportError:
        pass

    # ── IMAP email poller ─────────────────────────────────────
    t_poller = time()
    poller_task: asyncio.Task | None = None
    try:
        from services.email_poller import start_email_poller
        from dependencies import get_email_service
        poller_task = asyncio.create_task(start_email_poller(get_email_service()))
        print(f"⏱️  IMAP Poller: {time() - t_poller:.2f}s (✅ Started, polling every 15s)")
    except Exception as exc:
        print(f"IMAP Poller:    ⚠️  {exc}")

    # ── Hourly auto-reminder scheduler ───────────────────────
    t_scheduler = time()
    scheduler = None
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from services.auto_reminder import run_auto_reminders
        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.add_job(run_auto_reminders, "interval", hours=1, id="auto_reminders")
        scheduler.start()
        print(f"⏱️  Scheduler: {time() - t_scheduler:.2f}s (✅ Started)")
    except Exception as exc :
        print(f"Scheduler:      ⚠️  {exc}")

    total_time = time() - t0
    print(f"Sysco RFP AIQ backend running on port {settings.port}")
    print(f"⏱️  Total startup: {total_time:.2f}s")

    yield  # ← app serves requests

    # ── Shutdown ──────────────────────────────────────────────
    if poller_task:
        poller_task.cancel()
        try:
            await poller_task
        except asyncio.CancelledError:
            pass
    if scheduler:
        scheduler.shutdown(wait=False)


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Sysco RFP AIQ",
    version="1.0.0",
    description="AI-powered procurement tool for Sysco's Bid COE team",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# ── Middleware (order matters — applied bottom-up by Starlette) ──────────────

settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    APIKeyMiddleware,
    api_key=settings.internal_api_key.get_secret_value(),
)

app.add_middleware(RequestIDMiddleware)

# ── Static files ──────────────────────────────────────────────────────────────

UPLOADS_DIR.mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")

# ── Global exception handlers ─────────────────────────────────────────────────

@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": str(exc), "request_id": get_request_id()},
    )


@app.exception_handler(FileNotFoundError)
async def file_not_found_handler(request: Request, exc: FileNotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error": str(exc), "request_id": get_request_id()},
    )


@app.exception_handler(Exception)
async def generic_handler(request: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled_exception", error=str(exc), path=request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "request_id": get_request_id()},
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
@app.get("/api/v1/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    uptime = (datetime.now(timezone.utc) - _startup_time).total_seconds()
    return {
        "status": "ok",
        "service": "Sysco RFP AIQ Backend",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": round(uptime),
        "llmProvider": settings.llm_provider,
        "anthropicConfigured": bool(settings.anthropic_api_key.get_secret_value()),
        "emailProvider": settings.email_provider,
        "dbBackend": settings.db_backend,
    }


@app.get("/api/email/poll-now")
async def poll_now() -> dict[str, Any]:
    try:
        from services.email_poller import run_poll
        from dependencies import get_email_service
        result = await run_poll(get_email_service())
        return {"ok": True, **result, "timestamp": datetime.now(timezone.utc).isoformat()}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Router registration ───────────────────────────────────────────────────────

from routers.email_threads import router as emails_router
from routers.suppliers import router as suppliers_router
from routers.documents_v2 import router as documents_router

app.include_router(emails_router, prefix="/api/emails")
app.include_router(emails_router, prefix="/api/v1/emails")  # versioned alias
app.include_router(suppliers_router, prefix="/api/suppliers")
app.include_router(suppliers_router, prefix="/api/v1/suppliers")
app.include_router(documents_router, prefix="/api/documents")
app.include_router(documents_router, prefix="/api/v1/documents")

# AI + bids routers registered after Phase 2/3 modules are created
try:
    from routers.rfp_parser import router as rfp_router
    app.include_router(rfp_router, prefix="/api/parse-rfp")
    app.include_router(rfp_router, prefix="/api/v1/parse-rfp")
except ImportError:
    pass

try:
    from routers.pricing import router as pricing_router
    app.include_router(pricing_router, prefix="/api/extract-pricing")
    app.include_router(pricing_router, prefix="/api/v1/extract-pricing")
except ImportError:
    pass

try:
    from routers.analyse_response import router as analyse_router
    app.include_router(analyse_router, prefix="/api/analyse-response")
    app.include_router(analyse_router, prefix="/api/v1/analyse-response")
except ImportError:
    pass

try:
    # v2 adds the SQL Query Expert path (see docs/sql_expert_integration.md);
    # falls back to v1 behaviour when SQL_EXPERT_ENABLED is false.
    from routers.ai_chat_v2 import router as ai_router
    app.include_router(ai_router, prefix="/api/ai")
    app.include_router(ai_router, prefix="/api/v1/ai")
except ImportError:
    pass

try:
    from routers.bids import router as bids_router
    app.include_router(bids_router, prefix="/api/bids")
    app.include_router(bids_router, prefix="/api/v1/bids")
except ImportError:
    pass

try:
    from routers.submissions import router as submissions_router
    app.include_router(submissions_router, prefix="/api/submit")
    app.include_router(submissions_router, prefix="/api/v1/submit")
except ImportError:
    pass
