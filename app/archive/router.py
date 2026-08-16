"""Archive WebUI JSON API (same namespace as the rest of /api/*).

- GET    /api/archive                          list + stats
- GET    /api/archive/{gid}/{token}/quote      tier list + prices (no GP spent)
- POST   /api/archive/{gid}/{token}/start      trigger archive (free tiers free)
- GET    /api/archive/{gid}/{token}            single entry status
- GET    /api/archive/{gid}/{token}/metadata    persisted gdata snapshot
- POST   /api/archive/{gid}/{token}/metadata/refresh  force-refetch gdata + cover
- DELETE /api/archive/{gid}/{token}            delete local archive
- POST   /api/archive/{gid}/{token}/refresh    re-trigger re-download (no GP)

All routes require IPB cookies (archiver is a logged-in Star-member service);
the manager raises ArchiverUnavailableError (403) otherwise, mapped by the
global EHException handler. Deleting only removes local files — the E-Hentai
account archive record stays, so refresh/start re-downloads spend no GP.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=["archive"])


def _manager(request: Request):
    return request.app.state.archive


@router.get("/api/archive")
async def archive_list(request: Request):
    m = _manager(request)
    return {"entries": m.list_entries(), "stats": m.stats()}


@router.get("/api/archive/{gid}/{token}/quote")
async def archive_quote(request: Request, gid: int, token: str):
    return await _manager(request).quote(gid, token)


@router.post("/api/archive/{gid}/{token}/start")
async def archive_start(request: Request, gid: int, token: str, payload: dict | None = None):
    quality = None
    if isinstance(payload, dict):
        quality = payload.get("quality")
    return await _manager(request).start(gid, token, quality)


@router.get("/api/archive/{gid}/{token}")
async def archive_status(request: Request, gid: int, token: str):
    status = _manager(request).get_status(gid, token)
    if status is None or status.get("status") == "absent":
        raise HTTPException(status_code=404, detail="no archive entry for this gallery")
    return status


@router.get("/api/archive/{gid}/{token}/metadata")
async def archive_metadata(request: Request, gid: int, token: str):
    """Return the persisted gdata metadata snapshot (404 when absent)."""
    snap = await _manager(request).get_metadata_snapshot(gid, token)
    if snap is None:
        raise HTTPException(
            status_code=404, detail="no metadata snapshot for this gallery"
        )
    return snap


@router.delete("/api/archive/{gid}/{token}")
async def archive_delete(request: Request, gid: int, token: str):
    removed = await _manager(request).remove(gid, token)
    if not removed:
        raise HTTPException(status_code=404, detail="no archive entry for this gallery")
    return {"removed": True}


@router.post("/api/archive/{gid}/{token}/metadata/refresh")
async def archive_metadata_refresh(request: Request, gid: int, token: str):
    """Force-refetch gdata metadata + cover and overwrite the snapshot."""
    return await _manager(request).refresh_metadata(gid, token)


@router.post("/api/archive/{gid}/{token}/refresh")
async def archive_refresh(request: Request, gid: int, token: str):
    return await _manager(request).refresh(gid, token)
