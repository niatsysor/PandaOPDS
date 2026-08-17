"""FastAPI application entrypoint.

Run: uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .auth import auth_request_ok, unauthorized_response
from .archive.manager import ArchiveManager
from .archive.router import router as archive_router
from .archive.store import ArchiveStore
from .config import ConfigError, load_settings
from .eh.exceptions import EHException
from .eh.service import EHService
from .favorites.router import router as favorites_router
from .favorites.state import FavoritesSyncState
from .favorites.sync import FavoritesSyncer
from .opds.router import router as opds_router
from .opds2.router import router as opds2_router
from .stream.router import router as stream_router
from .throttle.limiter import CircuitOpenError
from .webui.router import router as webui_router
from . import __version__

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Keep the low-level HTTP client libraries quiet: they spam connection/TLS
# debug and duplicate every request as INFO. Outbound URLs are logged by
# app.eh.client (`EH outbound: ...`) instead.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
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

    # Archive manager: persistent GP-purchased zip masters (independent of the
    # disk LRU cache; enabled implicitly when IPB cookies are configured).
    archive_manager = ArchiveManager(
        settings,
        client=service.client,
        throttle=service.throttle,
        store=ArchiveStore(settings.archive_dir),
        service=service,
    )
    service.attach_archive(archive_manager)
    app.state.archive = archive_manager

    # Favorites syncer: periodic incremental scan (+ optional auto-archive).
    favorites_syncer = FavoritesSyncer(
        settings,
        service=service,
        archive=archive_manager,
        state=FavoritesSyncState(settings.favorites_sync_state),
    )
    app.state.favorites = favorites_syncer
    favorites_syncer.start()

    if settings.auth_enabled:
        exempt = ("/health", *settings.auth_exempt_paths)
        logger.info(
            "Basic Auth enabled: user=%s, %d exempt path(s)%s",
            settings.auth_username,
            len(settings.auth_exempt_paths),
            " " + ", ".join(exempt) if exempt else "",
        )
    elif settings.auth_username or settings.auth_password:
        # One-sided AUTH_* config: auth stays OFF. Failing open is safer than
        # locking the server out, but the operator should know the intent was
        # never honored.
        logger.warning(
            "AUTH_USERNAME/AUTH_PASSWORD set on one side only — Basic Auth "
            "NOT enabled (both must be set)"
        )

    logger.info(
        "PandaOPDS started: site=%s host=%s cache_dir=%s image_cache=%s",
        settings.eh_site,
        settings.site_host,
        settings.cache_dir,
        settings.image_cache_enabled,
    )
    yield
    favorites_syncer = getattr(app.state, "favorites", None)
    if favorites_syncer is not None:
        await favorites_syncer.stop()
    await service.close()


app = FastAPI(
    title="PandaOPDS",
    description="OPDS-PSE streaming server proxying E-Hentai.org",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(opds_router)
app.include_router(opds2_router)
app.include_router(stream_router)
app.include_router(archive_router)
app.include_router(favorites_router)
app.include_router(webui_router)


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    """Optional HTTP Basic Auth gate for every route.

    Off unless AUTH_USERNAME + AUTH_PASSWORD are both set; /health and any
    AUTH_EXEMPT_PATHS entry stay public. 401 replies carry the
    WWW-Authenticate challenge so browsers/OPDS clients prompt for
    credentials.
    """
    if not auth_request_ok(request):
        logger.info("basic auth rejected: %s %s", request.method, request.url.path)
        return unauthorized_response()
    return await call_next(request)


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
