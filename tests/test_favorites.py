"""Favorites feature tests (offline): favcat parsing, write-op proxying,
incremental scan, sync manager + auto-archive, and the /api/favorites routes.

Fixtures mirror the real favorites page markup (extended view): the
``div.nosel div.fp`` category picker carries the favcat map and each gallery's
posted element carries the folder name in its ``title`` attribute.
"""

import asyncio

import httpx
import pytest

from app.config import Settings
from app.eh.parser import parse_list_page, parse_favorites_categories
from app.eh.service import EHService
from app.favorites.state import FavoritesSyncState
from app.favorites.sync import FavoritesSyncer

FAVCAT_NAMES = {0: "All Favorites", 1: "Common", 2: "Videos"}


def _settings(**kw) -> Settings:
    base = dict(
        ipb_member_id="1",
        ipb_pass_hash="abc",
        favorites_sync_interval_seconds=0.0,
        favorites_sync_archive=False,
        favorites_sync_categories=(),
        favorites_sync_state="./favorites_sync_test.json",
    )
    base.update(kw)
    return Settings(**base)


def _fav_page(rows, *, picker=True, next_gid=None) -> str:
    """Build an extended-view favorites page. rows: (gid, token, title, favcat_name)."""
    rows_html = ""
    for gid, token, title, favcat_name in rows:
        rows_html += f"""
        <tr>
          <td class="gl1e" style="width:250px"><div><a href="https://e-hentai.org/g/{gid}/{token}/"><img src="/x/c{gid}.jpg"></a></div></td>
          <td class="gl2e"><div>
            <div class="gl3e">
              <div class="cn">Manga</div>
              <div onclick="popUp('https://e-hentai.org/gallerypopups.php?gid={gid}&amp;t={token}')" id="posted_{gid}" title="{favcat_name}">2026-08-12 00:00</div>
              <div class="ir" style="background-position:0px -21px;opacity:1"></div>
              <div><a href="https://e-hentai.org/uploader/u1">u1</a></div>
              <div>42 pages</div>
            </div>
            <a href="https://e-hentai.org/g/{gid}/{token}/"><div class="gl4e glname" style="min-height:100px">
              <div class="glink">{title}</div><div><table></table></div>
            </div></a>
          </div></td>
        </tr>"""
    picker_html = ""
    if picker:
        fp = "".join(
            f'<div class="fp" onclick="popUp(\'https://e-hentai.org/gallerypopups.php?gid=0&amp;t=0&amp;act=addfav&amp;favcat={i}\')">'
            f"<div>{i}</div><div>Favorite</div><div>{name}</div></div>"
            for i, name in FAVCAT_NAMES.items()
        )
        picker_html = f'<div class="nosel">{fp}</div>'
    nav = (
        f'<div class="searchnav"><a id="unext" href="?next={next_gid}">Next</a></div>'
        if next_gid else ""
    )
    return f"<html><body>{picker_html}<table class=\"itg glte\">{rows_html}</table>{nav}</body></html>"


class FakeClient:
    """EHClient stand-in for favorites: records write calls, serves pages."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.pages: dict[str, str] = {}
        self.session_calls = 0

    async def establish_session(self):
        self.session_calls += 1

    async def add_favorite(self, gid, token, favcat, note=""):
        self.calls.append(("add", gid, token, favcat, note))
        return "<html>ok</html>"

    async def move_favorite(self, gid, token, favcat, note=""):
        self.calls.append(("move", gid, token, favcat, note))
        return "<html>ok</html>"

    async def remove_favorite(self, gid, token):
        self.calls.append(("remove", gid, token))
        return "<html>ok</html>"

    async def get_html(self, path, params=None):
        if params:
            query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            full = f"{path}?{query}"
            if full in self.pages:
                return self.pages[full]
        return self.pages[path]


def make_service(tmp_path, **kw) -> tuple[Settings, FakeClient, EHService]:
    settings = _settings(cache_dir=tmp_path, **kw)
    client = FakeClient()
    service = EHService(settings, client=client)
    return settings, client, service


# --------------------------------------------------------------------------
# parser: favcat map + per-gallery favcat
# --------------------------------------------------------------------------


def test_parse_favcat_map_and_gallery_favcat():
    html = _fav_page([
        (100, "aaa", "New One", "Common"),
        (101, "bbb", "Old Two", "Videos"),
    ])
    info = parse_list_page(html)
    assert info.favcat_map == FAVCAT_NAMES
    by_gid = {g.gid: g for g in info.galleries}
    assert by_gid[100].favcat == 1          # "Common"
    assert by_gid[101].favcat == 2          # "Videos"
    # categories from raw html (public entry used by the service)
    assert parse_favorites_categories(html) == FAVCAT_NAMES


def test_non_favorites_page_has_empty_favcat():
    # same extended layout, no picker + no title attr -> favcat stays None
    html = _fav_page([(100, "aaa", "No Cat", "Common")], picker=False)
    info = parse_list_page(html)
    assert info.favcat_map == {}
    assert info.galleries[0].favcat is None


# --------------------------------------------------------------------------
# service: write ops
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_favorite_action_single(tmp_path):
    settings, client, service = make_service(tmp_path)
    results = await service.favorite_action(
        "add", [(100, "aaa")], favcat=2, note="hi"
    )
    assert results == [{"gid": 100, "token": "aaa", "ok": True}]
    assert client.calls == [("add", 100, "aaa", 2, "hi")]


@pytest.mark.asyncio
async def test_favorite_action_batch_and_remove(tmp_path):
    settings, client, service = make_service(tmp_path)
    results = await service.favorite_action(
        "move", [(100, "aaa"), (101, "bbb")], favcat=1
    )
    assert [r["ok"] for r in results] == [True, True]
    assert client.calls == [("move", 100, "aaa", 1, ""), ("move", 101, "bbb", 1, "")]

    client.calls.clear()
    results = await service.favorite_action("remove", [(100, "aaa")])
    assert results[0]["ok"] is True
    assert client.calls == [("remove", 100, "aaa")]


@pytest.mark.asyncio
async def test_favorite_action_invalidates_list_cache(tmp_path):
    settings, client, service = make_service(tmp_path)
    await service.mem.set("list:favorites:search:", {"cached": True})
    await service.mem.set("list:popular:", {"keep": True})
    await service.favorite_action("add", [(100, "aaa")], favcat=1)
    assert await service.mem.get("list:favorites:search:") is None
    assert await service.mem.get("list:popular:") is not None


@pytest.mark.asyncio
async def test_favorite_categories_cached(tmp_path):
    settings, client, service = make_service(tmp_path)
    client.pages["/favorites.php"] = _fav_page([(100, "aaa", "X", "Common")])
    cat1 = await service.favorite_categories()
    assert cat1 == FAVCAT_NAMES
    # cached: second call does not hit the client again
    client.pages.pop("/favorites.php")
    cat2 = await service.favorite_categories()
    assert cat2 == FAVCAT_NAMES


# --------------------------------------------------------------------------
# service: incremental scan
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_favorites_incremental_stop(tmp_path):
    settings, client, service = make_service(tmp_path)
    client.pages["/favorites.php"] = _fav_page([
        (100, "aaa", "New One", "Common"),      # new
        (101, "bbb", "Known One", "Common"),    # known
        (102, "ccc", "Known Two", "Common"),    # known -> threshold 2 reached
        (103, "ddd", "Never Seen", "Common"),
    ])
    result = await service.scan_favorites(
        {"101:bbb", "102:ccc"}, match_threshold=2, max_pages=5
    )
    assert [g.gid for g in result["new"]] == [100]
    assert sorted(result["seen"]) == [(100, "aaa"), (101, "bbb"), (102, "ccc")]
    assert result["pages"] == 1
    assert result["favcat_map"] == FAVCAT_NAMES


@pytest.mark.asyncio
async def test_scan_favorites_paginates(tmp_path):
    settings, client, service = make_service(tmp_path)
    client.pages["/favorites.php"] = _fav_page(
        [(100, "aaa", "New", "Common"), (101, "bbb", "Known", "Common")],
        next_gid=101,
    )
    client.pages["/favorites.php?inline_set=fs_f dm_e&next=101"] = _fav_page(
        [(102, "ccc", "Known2", "Common")]  # second known -> stop
    )
    result = await service.scan_favorites({"101:bbb", "102:ccc"}, match_threshold=2, max_pages=5)
    assert [g.gid for g in result["new"]] == [100]
    assert result["pages"] == 2
    # both pages must be fetched with the forced inline_set
    assert client.session_calls >= 2


@pytest.mark.asyncio
async def test_scan_favorites_whitelist(tmp_path):
    settings, client, service = make_service(tmp_path)
    client.pages["/favorites.php"] = _fav_page([
        (100, "aaa", "New Common", "Common"),     # favcat 1 (in scope)
        (200, "vvv", "Old Videos", "Videos"),     # favcat 2 (out of scope)
        (101, "bbb", "Known", "Common"),
    ])
    result = await service.scan_favorites(
        {"101:bbb"}, favcat_whitelist=(1,), match_threshold=1, max_pages=5
    )
    # out-of-scope favcat-2 gallery never counts (neither new nor a match)
    assert [g.gid for g in result["new"]] == [100]
    assert result["seen"] == [(100, "aaa"), (101, "bbb")]


@pytest.mark.asyncio
async def test_scan_favorites_max_pages_cap(tmp_path):
    settings, client, service = make_service(tmp_path)
    # every page returns a next link + only new galleries -> scan never stops
    html = _fav_page([(100, "aaa", "New", "Common")], next_gid=100)
    client.pages["/favorites.php"] = html
    result = await service.scan_favorites(set(), match_threshold=5, max_pages=2)
    assert result["pages"] == 2
    assert len(result["new"]) == 2  # same gid repeated across capped pages


# --------------------------------------------------------------------------
# sync manager
# --------------------------------------------------------------------------


class FakeScanService:
    """Stub for EHService.scan_favorites."""

    def __init__(self, result):
        self.result = result

    async def scan_favorites(self, known, **kw):
        return self.result


class FakeArchive:
    """Stub ArchiveManager: records start() calls; store never has entries."""

    def __init__(self):
        self.started: list[tuple[int, str]] = []
        self.fail_on: set[tuple[int, str]] = set()

    class store:
        @staticmethod
        def get(gid, token):
            return None

    async def start(self, gid, token):
        if (gid, token) in self.fail_on:
            raise RuntimeError("no GP")
        self.started.append((gid, token))


def _new_result(items, pages=1):
    return {
        "new": items,
        "seen": [(i.gid, i.token) for i in items],
        "favcat_map": FAVCAT_NAMES,
        "pages": pages,
    }


def _item(gid, token, title="T"):
    from app.eh.models import GalleryListItem

    return GalleryListItem(gid=gid, token=token, title=title, category="Manga", cover_url="")


def make_syncer(tmp_path, **kw) -> FavoritesSyncer:
    settings = _settings(favorites_sync_state=str(tmp_path / "state.json"), **kw)
    syncer = FavoritesSyncer(settings, service=FakeScanService(_new_result([])))
    return syncer


@pytest.mark.asyncio
async def test_sync_archive_off_records_known(tmp_path):
    syncer = make_syncer(tmp_path)
    syncer.service.result = _new_result([_item(100, "aaa")])
    out = await syncer.run()
    assert out["ok"] is True
    assert out["baseline"] is True
    assert len(out["new"]) == 1
    assert out["auto_archived"] == []
    assert "100:aaa" in syncer.state.known()
    assert syncer.state.archived() == set()
    # persisted
    assert FavoritesSyncState(tmp_path / "state.json").known() == {"100:aaa"}


@pytest.mark.asyncio
async def test_sync_archive_on_starts_archives_after_baseline(tmp_path):
    """First run is baseline (record only); auto-archive applies from the
    second run — the exact 'new items only' semantics."""
    syncer = make_syncer(tmp_path, favorites_sync_archive=True)
    syncer.archive = FakeArchive()
    # run 1: baseline — existing favorites recorded, NOT archived
    syncer.service.result = _new_result([_item(100, "aaa"), _item(101, "bbb")])
    out1 = await syncer.run()
    assert out1["baseline"] is True
    assert syncer.archive.started == []
    assert syncer.state.archived() == set()
    # run 2: a genuinely new item gets archived
    syncer.service.result = _new_result([_item(200, "ccc")])
    out2 = await syncer.run()
    assert out2["baseline"] is False
    assert syncer.archive.started == [(200, "ccc")]
    assert out2["auto_archived"] == ["200:ccc"]
    assert syncer.state.archived() == {"200:ccc"}


@pytest.mark.asyncio
async def test_sync_archive_dedup_skips_existing(tmp_path):
    syncer = make_syncer(tmp_path, favorites_sync_archive=True)
    archive = FakeArchive()
    syncer.archive = archive
    # baseline
    syncer.service.result = _new_result([_item(100, "aaa")])
    await syncer.run()
    # run 2: item 100 is now known (new item 200 appears)
    syncer.service.result = _new_result([_item(200, "ccc")])
    await syncer.run()
    assert archive.started == [(200, "ccc")]
    # run 3: nothing new -> nothing started again
    syncer.service.result = _new_result([])
    await syncer.run()
    assert archive.started == [(200, "ccc")]
    assert syncer.state.archived() == {"200:ccc"}


@pytest.mark.asyncio
async def test_sync_archive_error_is_recorded_not_fatal(tmp_path):
    syncer = make_syncer(tmp_path, favorites_sync_archive=True)
    archive = FakeArchive()
    archive.fail_on = {(200, "ccc")}
    syncer.archive = archive
    # baseline
    syncer.service.result = _new_result([_item(100, "aaa")])
    await syncer.run()
    # run 2: one item fails, the other is archived, run still ok
    syncer.service.result = _new_result([_item(200, "ccc"), _item(201, "ddd")])
    out = await syncer.run()
    assert "200:ccc" in out["errors"]
    assert archive.started == [(201, "ddd")]
    assert syncer.state.archived() == {"201:ddd"}
    assert "200:ccc" in syncer.state.errors()
    # a failed item is treated as known -> no retry on the next run
    syncer.service.result = _new_result([])
    await syncer.run()
    assert archive.started == [(201, "ddd")]


@pytest.mark.asyncio
async def test_sync_run_serialized(tmp_path):
    syncer = make_syncer(tmp_path)
    syncer.service.result = _new_result([_item(100, "aaa")])
    r1, r2 = await asyncio.gather(syncer.run(), syncer.run())
    assert r1["ok"] and r2["ok"]
    assert syncer.state.known() == {"100:aaa"}


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------


def _install_app_state(settings, service, syncer=None) -> None:
    from app.main import app

    app.state.settings = settings
    app.state.service = service
    app.state.favorites = syncer
    app.state.archive = getattr(syncer, "archive", None)


async def _post(path: str, payload: dict | None = None):
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=payload)


@pytest.mark.asyncio
async def test_route_add_single(tmp_path):
    settings, client, service = make_service(tmp_path)
    _install_app_state(settings, service)
    resp = await _post("/api/favorites", {
        "action": "add", "gid": 100, "token": "aaa", "favcat": 1, "note": "n"
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["ok_count"] == 1
    assert client.calls == [("add", 100, "aaa", 1, "n")]


@pytest.mark.asyncio
async def test_route_batch_and_validation(tmp_path):
    settings, client, service = make_service(tmp_path)
    _install_app_state(settings, service)
    resp = await _post("/api/favorites", {
        "action": "remove", "items": [{"gid": 1, "token": "a"}, {"gid": 2, "token": "b"}]
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert len(client.calls) == 2

    # missing action
    resp = await _post("/api/favorites", {"gid": 1, "token": "a"})
    assert resp.status_code == 400
    # missing favcat for add
    resp = await _post("/api/favorites", {"action": "add", "gid": 1, "token": "a"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_route_requires_ipb(tmp_path):
    settings = _settings(cache_dir=tmp_path, ipb_member_id="", ipb_pass_hash="")
    service = EHService(settings)
    _install_app_state(settings, service)
    resp = await _post("/api/favorites", {"action": "add", "gid": 1, "token": "a", "favcat": 1})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_route_sync_run(tmp_path):
    settings, client, service = make_service(tmp_path)
    syncer = make_syncer(tmp_path)
    syncer.service.result = _new_result([_item(100, "aaa")])
    _install_app_state(settings, service, syncer=syncer)
    resp = await _post("/api/favorites/sync/run")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["baseline"] is True
    assert "100:aaa" in syncer.state.known()
