"""FastAPI application entry point."""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middleware.exception_mw import ExceptionMiddleware
from app.api.middleware.logging_mw import LoggingMiddleware
from app.api.middleware.request_id import RequestIDMiddleware
from app.api.v1.ext import ExtApiStatsMiddleware
from app.api.v1.router import api_v1_router
from app.core.bootstrap import background_boot, init_critical_path, shutdown
from app.core.config import settings
from app.core.logging import setup_logging

# Initialize structured logging before app creation
setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup/shutdown lifecycle for external connections.

    Startup is split into:
    1. **Critical path** (awaited): checkpointer, notification, event bridge.
    2. **Background boot** (create_task): indexes, schedulers, recovery,
       channel connections — deferred so the first request isn't blocked.
    """
    # Critical path — must complete before serving requests.
    await init_critical_path()

    # Background boot — indexes/schedulers/recovery/channels.
    # Store the task reference on app.state to prevent GC, and to allow
    # graceful shutdown ordering (cancel before closing DB clients).
    boot_task = asyncio.create_task(background_boot())
    app.state._boot_task = boot_task

    # Capture schedulers when background boot completes (best-effort;
    # if it hasn't finished by shutdown, we cancel it).
    app.state._scheduler = None
    app.state._trigger_scheduler = None

    async def _capture_schedulers():
        try:
            scheduler, trigger_scheduler = await boot_task
            app.state._scheduler = scheduler
            app.state._trigger_scheduler = trigger_scheduler
        except Exception as exc:
            from loguru import logger
            logger.error("background_boot_failed error={}", exc)

    asyncio.create_task(_capture_schedulers())

    yield

    # Shutdown — cancel background boot if still running, then graceful stop.
    if not boot_task.done():
        boot_task.cancel()
    await shutdown(app.state._scheduler, app.state._trigger_scheduler)


app = FastAPI(
    title=settings.APP_NAME,
    description="Agent Flow - AI Agent orchestration platform",
    version="0.1.0",
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
    openapi_url="/api/v1/openapi.json",
    lifespan=lifespan,
)

# Middleware order matters: outermost first (executed first on request, last on response)
app.add_middleware(ExceptionMiddleware)
app.add_middleware(LoggingMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"],
)
app.add_middleware(ExtApiStatsMiddleware)

# API routes
app.include_router(api_v1_router, prefix="/api/v1")


@app.get("/", tags=["root"])
async def root() -> dict[str, str]:
    """Root endpoint - redirects users to docs."""
    return {
        "message": f"Welcome to {settings.APP_NAME}",
        "docs": "/api/v1/docs",
        "redoc": "/api/v1/redoc",
    }


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    """Liveness probe (also exposed at /api/v1/health for consistency)."""
    return {"status": "ok"}
