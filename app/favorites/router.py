"""Favorites JSON API (under /api/favorites; follows the /api/archive pattern).

Write ops mirror the EH gallerypopups form (gid/token/act=addfav in the URL,
favcat/favnote/apply/update in the body — see ``app/eh/client.py``).

Routes:
- POST   /api/favorites                unified write action (add|move|remove),
                                       accepts single shorthand (gid+token at
                                       the top level) or a batch ``items`` list
- GET    /api/favorites/categories    favorites folder list (id + name)
- GET    /api/favorites/sync          sync state (periodic + auto-archive cfg)
- POST   /api/favorites/sync/run      trigger one scan now (shortcut-friendly)

All routes require IPB cookies (favorites are account-scoped and the write
forms need the logged-in session); a missing login returns 403. Batch size is
capped (sequential per-item POSTs — a huge batch would block the request for
minutes).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=["favorites"])

MAX_BATCH_ITEMS = 200
ACTIONS = ("add", "move", "remove")


def _service(request: Request):
    return request.app.state.service


def _syncer(request: Request):
    return getattr(request.app.state, "favorites", None)


def _require_ipb(request: Request) -> None:
    settings = request.app.state.settings
    if not (settings.ipb_member_id and settings.ipb_pass_hash):
        raise HTTPException(
            status_code=403,
            detail="favorites require IPB cookies (IPB_MEMBER_ID + IPB_PASS_HASH)",
        )


def _parse_items(payload: dict) -> list[tuple[int, str]]:
    """Extract (gid, token) pairs from either the batch ``items`` list or the
    single top-level ``gid`` + ``token`` shorthand."""
    items_raw = payload.get("items")
    if items_raw is None:
        gid = payload.get("gid")
        token = payload.get("token")
        if gid is None or not token:
            raise HTTPException(
                status_code=400,
                detail="provide either 'items' [{gid, token}, ...] or a single "
                       "'gid' + 'token'",
            )
        items_raw = [{"gid": gid, "token": token}]

    if not isinstance(items_raw, list) or not items_raw:
        raise HTTPException(status_code=400, detail="'items' must be a non-empty list")
    if len(items_raw) > MAX_BATCH_ITEMS:
        raise HTTPException(
            status_code=400,
            detail=f"batch too large ({len(items_raw)} > {MAX_BATCH_ITEMS}); "
                   "split into chunks",
        )

    items: list[tuple[int, str]] = []
    for entry in items_raw:
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail="each item must be an object")
        gid = entry.get("gid")
        token = entry.get("token")
        if gid is None or not token:
            raise HTTPException(status_code=400, detail="each item needs gid + token")
        try:
            items.append((int(gid), str(token)))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"invalid gid {gid!r}")
    return items


def _parse_favcat(payload: dict, action: str) -> int | str | None:
    """favcat required for add/move; ignored for remove."""
    if action == "remove":
        return None
    favcat = payload.get("favcat")
    if favcat is None:
        raise HTTPException(status_code=400, detail="'favcat' is required for add/move")
    try:
        return int(favcat)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"invalid favcat {favcat!r}")


@router.post("/api/favorites")
async def favorites_action(request: Request, payload: dict):
    """Proxy a favorites write op (add | move | remove) to E-Hentai.

    Single:  ``POST /api/favorites`` with ``{action, gid, token, favcat, note}``
    Batch:   ``POST /api/favorites`` with
             ``{action, favcat, items: [{gid, token}, ...]}``
    """
    _require_ipb(request)

    action = (payload.get("action") or "").strip().lower()
    if action not in ACTIONS:
        raise HTTPException(
            status_code=400, detail=f"'action' must be one of {ACTIONS}"
        )
    items = _parse_items(payload)
    favcat = _parse_favcat(payload, action)
    note = str(payload.get("note") or "")

    results = await _service(request).favorite_action(
        action, items, favcat=favcat, note=note
    )
    # any successful write → schedule one (debounced, coalesced) background
    # scan so the favorites index / auto-archive catches up immediately
    # instead of waiting for the next periodic tick.
    syncer = _syncer(request)
    if syncer is not None and any(r["ok"] for r in results):
        syncer.request_run()
    return {
        "action": action,
        "ok": all(r["ok"] for r in results),
        "ok_count": sum(1 for r in results if r["ok"]),
        "items": results,
    }


@router.get("/api/favorites/categories")
async def favorites_categories(request: Request):
    _require_ipb(request)
    cat = await _service(request).favorite_categories()
    return {
        "categories": [
            {"id": fav_id, "name": name}
            for fav_id, name in sorted(cat.items())
        ]
    }


@router.get("/api/favorites/sync")
async def favorites_sync_status(request: Request):
    syncer = _syncer(request)
    if syncer is None:
        raise HTTPException(status_code=404, detail="favorites sync unavailable")
    return syncer.status()


@router.post("/api/favorites/sync/run")
async def favorites_sync_run(request: Request):
    _require_ipb(request)
    syncer = _syncer(request)
    if syncer is None:
        raise HTTPException(status_code=404, detail="favorites sync unavailable")
    return await syncer.run()