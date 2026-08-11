"""FastAPI application entrypoint.

Run: uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import ConfigError, load_settings
from .eh.exceptions import EHException
from .eh.service import EHService
from .opds.router import router as opds_router
from .stream.router import router as stream_router
from .throttle.limiter import CircuitOpenError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    try:
        settings.validate()
        app.state.config_ok = True
        app.state.config_error = None
    except ConfigError as exc:
        app.state.config_ok = False
        app.state.config_error = str(exc)
        logger.error("CONFIG ERROR: %s", exc)
        logger.error(
            "Set IPB_MEMBER_ID and IPB_PASS_HASH (see AGENTS.md 鉴权 Cookie section) "
            "or run with a .env file."
        )

    service = EHService(settings)
    app.state.settings = settings
    app.state.service = service
    logger.info(
        "EHOPDS started: site=%s host=%s cache_dir=%s image_cache=%s",
        settings.eh_site,
        settings.site_host,
        settings.cache_dir,
        settings.image_cache_enabled,
    )
    yield
    await service.close()


app = FastAPI(
    title="EHOPDS",
    description="OPDS-PSE streaming server proxying E-Hentai.org",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(opds_router)
app.include_router(stream_router)


@app.get("/health")
async def health(request: Request):
    service: EHService = request.app.state.service
    settings = request.app.state.settings
    return {
        "status": "ok" if request.app.state.config_ok else "degraded",
        "config_error": request.app.state.config_error,
        "site": settings.eh_site,
        "host": settings.site_host,
        "public_base_url": settings.public_base_url or "(relative URLs)",
        "stats": await service.stats(),
    }


@app.exception_handler(EHException)
async def eh_exception_handler(request: Request, exc: EHException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": type(exc).__name__, "detail": str(exc)},
    )


@app.exception_handler(CircuitOpenError)
async def circuit_open_handler(request: Request, exc: CircuitOpenError):
    headers = {}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(int(exc.retry_after))
    return JSONResponse(
        status_code=503,
        content={"error": "upstream_throttled", "detail": exc.reason},
        headers=headers,
    )
