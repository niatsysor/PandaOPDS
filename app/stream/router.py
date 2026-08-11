"""PSE stream + thumbnail proxy routes.

- GET /stream/{gid}/{token}/page/{n}  (n is 0-based per OPDS-PSE)
- GET /image/{gid}/{token}/thumb
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..eh.exceptions import ExceedLimitError
from ..eh.service import EHService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stream"])

# images are immutable per (gid, token, page): long public cache
_IMAGE_CACHE = "public, max-age=604800"


def _service(request: Request) -> EHService:
    return request.app.state.service


@router.get("/stream/{gid}/{token}/page/{page}")
async def stream_page(request: Request, gid: int, token: str, page: int):
    service = _service(request)
    base = request.app.state.settings.pse_page_base
    if page < base:
        raise HTTPException(
            status_code=400,
            detail=f"page must be >= {base} (PSE page base)",
        )
    try:
        data, mime = await service.get_image(gid, token, page)
    except ExceedLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": _IMAGE_CACHE},
    )


@router.get("/image/{gid}/{token}/thumb")
async def thumb(request: Request, gid: int, token: str):
    service = _service(request)
    data, mime = await service.get_thumb(gid, token)
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": _IMAGE_CACHE},
    )
