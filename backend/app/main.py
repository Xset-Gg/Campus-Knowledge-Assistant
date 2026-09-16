"""Campus Knowledge Assistant — FastAPI application entrypoint."""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.db import engine
from app.observability import tracing
from app.routers import admin, auth_router, chat, documents, feedback

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Warm the ML models at startup so the first user request is not the slow one."""
    import asyncio

    from app.services import embedding_service, rerank_service

    logger.info("Starting Campus Knowledge Assistant (%s)", settings.environment)
    await asyncio.gather(
        asyncio.to_thread(embedding_service.warm_up),
        asyncio.to_thread(rerank_service.warm_up),
    )
    logger.info("Models ready")
    yield
    tracing.flush()
    await engine.dispose()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Campus Knowledge Assistant API",
    description=(
        "RAG assistant answering university policy and syllabus questions with "
        "page-level citations, under role-based access control."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a request id and latency header to every response."""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.1f}"
    if elapsed_ms > 5000:
        logger.warning("Slow request %s %s took %.0fms", request.method, request.url.path, elapsed_ms)
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Invalid request", "errors": exc.errors()},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Log the detail server-side; return a generic message so internal state
    # (SQL, file paths, stack frames) never reaches the client.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal error occurred"},
    )


app.include_router(auth_router.router)
app.include_router(chat.router)
app.include_router(documents.router)
app.include_router(feedback.router)
app.include_router(admin.router)


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "environment": settings.environment}


@app.get("/health/ready", tags=["system"])
async def readiness() -> JSONResponse:
    """Readiness probe — verifies the database and the pgvector extension."""
    checks: dict[str, str] = {}
    healthy = True

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            has_vector = (
                await connection.execute(
                    text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                )
            ).first()
        checks["database"] = "ok"
        checks["pgvector"] = "ok" if has_vector else "missing"
        healthy = bool(has_vector)
    except Exception as exc:
        logger.exception("Readiness check failed")
        checks["database"] = f"error: {type(exc).__name__}"
        healthy = False

    return JSONResponse(
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ready" if healthy else "not ready", "checks": checks},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.api_host, port=settings.api_port, reload=True)
