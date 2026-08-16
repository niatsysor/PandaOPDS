"""Tests for the archive feature: archiver-page parsing + manager lifecycle.

Fixtures mirror the real archiver.php markup (tier forms with dltype/dlcheck,
hath.network status pages, ?start=1 download links).
"""

import asyncio
import io
import time
import zipfile

import pytest

from app.eh.parser import parse_archiver_page

# --------------------------------------------------------------------------
# archiver.php fixtures (real markup)
# --------------------------------------------------------------------------

TIERS_HTML = """
<!DOCTYPE html><html><body><div id="db">
<h1>Test Gallery Title [Artist]</h1>
<p>Current Funds:</p><p>161,479,373 GP [<a href="#">?</a>]</p>
<div style="width:180px; float:left">
  <div>Download Cost: &nbsp; <strong>Free!</strong></div>
  <form action="https://e-hentai.org/archiver.php?gid=4122641&amp;token=15037e1d97" method="post">
    <input type="hidden" name="dltype" value="org" />
    <div><input type="submit" name="dlcheck" value="Download Original Archive" /></div>
  </form>
  <p>Estimated Size: &nbsp; <strong>298.7 MiB</strong></p>
</div>
<div style="width:180px; float:right">
  <div>Download Cost: &nbsp; <strong>315,000 GP</strong></div>
  <form action="https://e-hentai.org/archiver.php?gid=4122641&amp;token=15037e1d97" method="post">
    <input type="hidden" name="dltype" value="res" />
    <div><input type="submit" name="dlcheck" value="Download Resample Archive" disabled="disabled" /></div>
  </form>
  <p>Estimated Size: &nbsp; <strong>N/A</strong></p>
</div>
</div></body></html>
"""

UNLOCKED_HTML = """
<!DOCTYPE html><html><body><div id="db">
<h1>Unlocked Gallery</h1>
<div>Download Cost: &nbsp; <strong>Free!</strong></div>
<form action="https://e-hentai.org/archiver.php?gid=1&amp;token=t" method="post">
  <input type="hidden" name="dltype" value="org" />
  <div><input type="submit" name="dlcheck" value="Download Original Archive" /></div>
</form>
<p>You unlocked an original download of this archive on 2026-08-16 09:20</p>
</div></body></html>
"""

TIERS_RES_HTML = """
<!DOCTYPE html><html><body><div id="db">
<h1>Res Tiers Gallery</h1>
<div>Download Cost: &nbsp; <strong>315,000 GP</strong></div>
<form action="https://e-hentai.org/archiver.php?gid=1&amp;token=t" method="post">
  <input type="hidden" name="dltype" value="res" />
  <div><input type="submit" name="dlcheck" value="Download Resample Archive" /></div>
</form>
</div></body></html>
"""

PREPARING_HTML = """
<!DOCTYPE html><html><body><div id="db">
<p>Locating archive server and preparing file for download...</p>
<p>(this can take several minutes)</p>
<p id="continue">(<a href="https://encvgvvzml.hath.network/archive/4122641/hash/abc/2">Click here if your browser does not continue automatically</a>)</p>
</div></body></html>
"""

READY_HTML = """
<!DOCTYPE html><html><body><div id="db">
<p>The file was successfully prepared, and is ready for download.<br /><br />
<strong>Test Gallery.zip</strong><br /><br />
<a href="/archive/4122641/hash/abc/2?start=1">Click here to download</a></p>
</div></body></html>
"""

NOT_MEMBER_HTML = """
<!DOCTYPE html><html><body><div id="db">
<p>You must be a Star member to use the archiver.</p>
</div></body></html>
"""

STATUS_URL = "https://encvgvvzml.hath.network/archive/4122641/hash/abc/2"
DL_URL = "https://encvgvvzml.hath.network/archive/4122641/hash/abc/2?start=1"


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def test_tiers_options_and_prices():
    info = parse_archiver_page(TIERS_HTML)
    assert info.title == "Test Gallery Title [Artist]"
    assert info.gp_balance == 161479373
    assert info.error == ""
    assert info.download_state == ""
    assert len(info.options) == 2
    org = info.options[0]
    assert org.or_value == "org"
    assert org.label == "Original Archive"
    assert org.dlcheck == "Download Original Archive"
    assert org.gp_price == 0  # Free!
    assert org.size == "298.7 MiB"
    assert org.available is True
    res = info.options[1]
    assert res.or_value == "res"
    assert res.gp_price == 315000
    assert res.available is False  # disabled


def test_unlocked_flag():
    info = parse_archiver_page(UNLOCKED_HTML)
    assert info.options[0].unlocked is True


def test_preparing_state_extracts_hath_url():
    info = parse_archiver_page(PREPARING_HTML)
    assert info.download_state == "preparing"
    assert info.download_url == STATUS_URL


def test_ready_state_extracts_download_link():
    info = parse_archiver_page(READY_HTML, page_url=STATUS_URL)
    assert info.download_state == "ready"
    assert info.download_url == DL_URL


def test_not_member_error():
    info = parse_archiver_page(NOT_MEMBER_HTML)
    assert info.error == "Archiver requires Star membership"
    assert info.options == []


# --------------------------------------------------------------------------
# manager (mock client, real store/throttle)
# --------------------------------------------------------------------------

from app.archive import manager as manager_mod  # noqa: E402
from app.archive.manager import ArchiveManager  # noqa: E402
from app.archive.store import ArchiveStore, ST_FAILED, ST_READY  # noqa: E402
from app.config import Settings  # noqa: E402
from app.eh.exceptions import ArchiverUnavailableError  # noqa: E402
from app.throttle.limiter import Throttle  # noqa: E402


class FakeClient:
    """Minimal EHClient stand-in: scripted pages / URLs / archive bytes."""

    def __init__(self):
        self.pages = {}      # ("get"|"submit", gid, token) -> html
        self.urls = {}       # url -> html (hath status/ready pages)
        self.archives = {}   # url -> bytes (final download)
        self.submits = []
        self.session_calls = 0

    async def establish_session(self):
        self.session_calls += 1

    async def get_archiver_page(self, gid, token):
        return self.pages[("get", gid, token)]

    async def submit_archiver(self, gid, token, dltype, dlcheck):
        self.submits.append((gid, token, dltype, dlcheck))
        return self.pages[("submit", gid, token)]

    async def get_absolute_html(self, url, **kw):
        return self.urls[url]

    async def stream_archive(self, url, dest, *, progress_cb=None, offset=0):
        data = self.archives[url]
        dest.parent.mkdir(parents=True, exist_ok=True)
        if offset and offset < len(data):
            with dest.open("ab") as fh:
                fh.write(data[offset:])
            n = len(data)
        else:
            dest.write_bytes(data)
            n = len(data)
        if progress_cb:
            progress_cb(n)
        return n


def make_manager(tmp_path, **overrides):
    kwargs = {
        "ipb_member_id": "1",
        "ipb_pass_hash": "h",
        "archive_dir": tmp_path / "archives",
        "archive_download_concurrency": 2,
    }
    kwargs.update(overrides)
    settings = Settings(**kwargs)
    client = FakeClient()
    manager = ArchiveManager(
        settings, client=client, throttle=Throttle(settings),
        store=ArchiveStore(settings.archive_dir),
    )
    return settings, client, manager


def zip_bytes(names, payload=b"page-"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i, name in enumerate(names):
            zf.writestr(name, payload + str(i).encode())
    return buf.getvalue()


async def wait_done(manager, gid, token, timeout=5.0):
    key = manager.store.key(gid, token)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = manager._tasks.get(key)
        if t is None or t.done():
            if t is not None and not t.cancelled():
                t.result()  # surface task exceptions
            return
        await asyncio.sleep(0.01)
    raise AssertionError("archive task did not finish in time")


def wire_archive(client, gid=1, token="t", names=("a.jpg", "b.jpg", "c.jpg"),
                 tier=("org", "Download Original Archive")):
    """Wire the full mock chain: tiers page -> submit -> preparing -> ready -> zip."""
    client.pages[("get", gid, token)] = TIERS_HTML
    client.pages[("submit", gid, token)] = PREPARING_HTML
    client.urls[STATUS_URL] = READY_HTML
    client.archives[DL_URL] = zip_bytes(names)
    return tier


@pytest.mark.asyncio
async def test_start_requires_ipb(tmp_path):
    settings, client, manager = make_manager(tmp_path, ipb_member_id="", ipb_pass_hash="")
    with pytest.raises(ArchiverUnavailableError):
        await manager.start(1, "t")


@pytest.mark.asyncio
async def test_quote_returns_options(tmp_path):
    _, client, manager = make_manager(tmp_path)
    client.pages[("get", 1, "t")] = TIERS_HTML
    quote = await manager.quote(1, "t")
    assert quote["title"] == "Test Gallery Title [Artist]"
    assert quote["gp_balance"] == 161479373
    assert len(quote["options"]) == 2
    org = next(o for o in quote["options"] if o["or"] == "org")
    assert org["gp_price"] == 0 and org["available"] is True
    res = next(o for o in quote["options"] if o["or"] == "res")
    assert res["gp_price"] == 315000 and res["available"] is False


@pytest.mark.asyncio
async def test_start_full_lifecycle_to_ready(tmp_path):
    _, client, manager = make_manager(tmp_path)
    wire_archive(client)

    st = await manager.start(1, "t")  # default quality -> original -> org
    assert st["status"] == "pending"
    assert client.submits == [(1, "t", "org", "Download Original Archive")]
    await wait_done(manager, 1, "t")

    meta = manager.get_status(1, "t")
    assert meta["status"] == ST_READY
    assert meta["page_count"] == 3
    assert meta["quality"] == "Original Archive"
    # pages readable from the archived zip
    assert await manager.get_page_bytes(1, "t", 2) == b"page-1"
    assert await manager.get_page_bytes(1, "t", 4) is None
    # no .part left behind
    assert not manager.store.part_path(1, "t").exists()


@pytest.mark.asyncio
async def test_start_specific_quality(tmp_path):
    _, client, manager = make_manager(tmp_path)
    wire_archive(client, names=("p1.jpg",))
    client.pages[("get", 1, "t")] = TIERS_RES_HTML  # res available here
    await manager.start(1, "t", quality="res")
    assert client.submits == [(1, "t", "res", "Download Resample Archive")]
    await wait_done(manager, 1, "t")
    assert manager.get_status(1, "t")["status"] == ST_READY


@pytest.mark.asyncio
async def test_quality_matches_label_alias(tmp_path):
    _, client, manager = make_manager(tmp_path)
    wire_archive(client, names=("p1.jpg",))
    await manager.start(1, "t", quality="original")  # alias -> org
    assert client.submits[0][2] == "org"
    await wait_done(manager, 1, "t")


@pytest.mark.asyncio
async def test_7z_converted_to_zip_ready(tmp_path):
    pytest.importorskip("py7zr")
    import py7zr

    buf = io.BytesIO()
    with py7zr.SevenZipFile(buf, "w") as z:
        z.writestr(b"seven-0", "x.jpg")
        z.writestr(b"seven-1", "y.jpg")
    seven_bytes = buf.getvalue()

    _, client, manager = make_manager(tmp_path)
    client.pages[("get", 1, "t")] = TIERS_HTML
    client.pages[("submit", 1, "t")] = PREPARING_HTML
    client.urls[STATUS_URL] = READY_HTML
    client.archives[DL_URL] = seven_bytes

    await manager.start(1, "t")
    await wait_done(manager, 1, "t")

    meta = manager.get_status(1, "t")
    assert meta["status"] == ST_READY
    assert meta["page_count"] == 2
    assert await manager.get_page_bytes(1, "t", 1) == b"seven-0"
    assert await manager.get_page_bytes(1, "t", 2) == b"seven-1"


@pytest.mark.asyncio
async def test_unknown_format_fails(tmp_path):
    _, client, manager = make_manager(tmp_path)
    client.pages[("get", 1, "t")] = TIERS_HTML
    client.pages[("submit", 1, "t")] = PREPARING_HTML
    client.urls[STATUS_URL] = READY_HTML
    client.archives[DL_URL] = b"definitely not an archive"

    await manager.start(1, "t")
    await wait_done(manager, 1, "t")

    meta = manager.get_status(1, "t")
    assert meta["status"] == ST_FAILED
    assert "format" in (meta["error"] or "")


@pytest.mark.asyncio
async def test_remove_cancels_and_deletes(tmp_path):
    _, client, manager = make_manager(tmp_path)
    wire_archive(client)
    await manager.start(1, "t")
    await wait_done(manager, 1, "t")
    assert manager.get_status(1, "t")["status"] == ST_READY

    assert await manager.remove(1, "t") is True
    assert manager.get_status(1, "t")["status"] == "absent"
    assert not (tmp_path / "archives" / "1").exists()


@pytest.mark.asyncio
async def test_refresh_retriggers_download(tmp_path):
    _, client, manager = make_manager(tmp_path)
    wire_archive(client)
    await manager.start(1, "t")
    await wait_done(manager, 1, "t")
    assert len(client.submits) == 1

    await manager.refresh(1, "t")
    assert len(client.submits) == 2  # re-POST same tier
    await wait_done(manager, 1, "t")
    assert manager.get_status(1, "t")["status"] == ST_READY


# --------------------------------------------------------------------------
# service integration: /stream serves archived pages first
# --------------------------------------------------------------------------

from app.cache.disk import DiskImageCache  # noqa: E402
from app.eh.service import EHService  # noqa: E402


class NoUpstreamClient:
    """Any upstream call is a test failure: archive/disk must cover us."""

    async def establish_session(self):
        raise AssertionError("upstream session established")

    async def get_html(self, path, **kw):
        raise AssertionError("upstream HTML requested: " + str(path))

    async def get_absolute_html(self, url, **kw):
        raise AssertionError("upstream absolute HTML requested: " + url)

    async def post_api(self, payload):
        raise AssertionError("upstream API requested")

    async def fetch_image_bytes(self, url, **kw):
        raise AssertionError("upstream image requested: " + url)


class FakeArchive:
    """Archive manager stand-in: page 1 archived only."""

    async def get_page_bytes(self, gid, token, page_no_1):
        return b"ARCHIVE" if page_no_1 == 1 else None


@pytest.mark.asyncio
async def test_get_image_prefers_archive_over_disk(tmp_path):
    service = EHService(
        Settings(pse_page_base=1, image_cache_enabled=True),
        client=NoUpstreamClient(),
        disk_cache=DiskImageCache(tmp_path, max_gb=1.0, ttl_seconds=3600),
    )
    await service.disk.put(1, "t", 1, b"DISK")
    service.attach_archive(FakeArchive())

    data, mime = await service.get_image(1, "t", 1)
    assert data == b"ARCHIVE"  # archived page wins over the disk LRU
    assert mime == "application/octet-stream"


@pytest.mark.asyncio
async def test_get_image_archive_miss_falls_through_to_upstream(tmp_path):
    service = EHService(
        Settings(pse_page_base=1, image_cache_enabled=True),
        client=NoUpstreamClient(),
        disk_cache=DiskImageCache(tmp_path, max_gb=1.0, ttl_seconds=3600),
    )
    service.attach_archive(FakeArchive())
    # page 2 is not archived and not in the disk cache -> upstream must be hit
    with pytest.raises(AssertionError, match="upstream"):
        await service.get_image(1, "t", 2)


@pytest.mark.asyncio
async def test_get_image_pse_zero_based_maps_to_archive(tmp_path):
    service = EHService(
        Settings(pse_page_base=0, image_cache_enabled=True),
        client=NoUpstreamClient(),
        disk_cache=DiskImageCache(tmp_path, max_gb=1.0, ttl_seconds=3600),
    )
    service.attach_archive(FakeArchive())
    # PSE page 0 == EH page 1 -> archived
    data, _ = await service.get_image(1, "t", 0)
    assert data == b"ARCHIVE"
