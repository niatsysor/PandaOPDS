"""Unit tests for the persistent archive store (pure local, no network)."""

import asyncio
import json
import zipfile

import pytest

from app.archive.store import (
    ST_READY,
    ArchiveStore,
)


@pytest.mark.asyncio
async def test_upsert_get_roundtrip(tmp_path):
    store = ArchiveStore(tmp_path)
    assert store.get(1, "tok") is None

    await store.upsert(1, "tok", {"title": "G1", "status": ST_READY})
    meta = store.get(1, "tok")
    assert meta["gid"] == 1
    assert meta["token"] == "tok"
    assert meta["title"] == "G1"
    assert meta["status"] == ST_READY
    assert meta["created_at"] > 0
    assert meta["archive_size"] == 0

    # unknown patch keys are dropped (whitelist)
    await store.upsert(1, "tok", {"status": "downloading", "bogus": 1})
    assert store.get(1, "tok")["status"] == "downloading"
    assert "bogus" not in store.get(1, "tok")


@pytest.mark.asyncio
async def test_atomic_write_no_tmp_leftover(tmp_path):
    store = ArchiveStore(tmp_path)
    await store.upsert(5, "ab", {"status": "pending"})
    assert not list(tmp_path.rglob("*.tmp"))
    # meta persisted to disk
    meta_file = store.meta_path(5, "ab")
    raw = json.loads(meta_file.read_text(encoding="utf-8"))
    assert raw["gid"] == 5 and raw["status"] == "pending"


@pytest.mark.asyncio
async def test_scan_rebuilds_index(tmp_path):
    store = ArchiveStore(tmp_path)
    await store.upsert(1, "a", {"status": ST_READY})
    await store.upsert(2, "b", {"status": "downloading"})

    store2 = ArchiveStore(tmp_path)  # fresh instance rescans the directory
    assert len(store2.list_entries()) == 2
    assert store2.get(2, "b")["status"] == "downloading"


@pytest.mark.asyncio
async def test_scan_skips_corrupt_meta(tmp_path):
    store = ArchiveStore(tmp_path)
    await store.upsert(1, "good", {"status": ST_READY})
    # corrupt meta + a meta missing gid/token
    bad_dir = tmp_path / "9" / "bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "meta.json").write_text("{not json", encoding="utf-8")
    nodir = tmp_path / "10" / "none"
    nodir.mkdir(parents=True)
    (nodir / "meta.json").write_text(json.dumps({"title": "x"}), encoding="utf-8")

    store2 = ArchiveStore(tmp_path)
    entries = store2.list_entries()
    assert len(entries) == 1
    assert entries[0]["gid"] == 1
    # corrupt files still on disk (not deleted)
    assert (bad_dir / "meta.json").exists()


@pytest.mark.asyncio
async def test_remove_deletes_whole_entry(tmp_path):
    store = ArchiveStore(tmp_path)
    await store.upsert(1, "tok", {"status": ST_READY})
    # drop a zip so the entry has real files
    zip_path = store.zip_path(1, "tok")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.write_bytes(b"PK fake")

    assert await store.remove(1, "tok") is True
    assert store.get(1, "tok") is None
    assert not (tmp_path / "1").exists()
    assert await store.remove(1, "tok") is False  # second remove no-op


def _make_zip(path, names):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for i, name in enumerate(names):
            zf.writestr(name, f"page-{i}".encode())


@pytest.mark.asyncio
async def test_zip_page_reads_in_order(tmp_path):
    store = ArchiveStore(tmp_path)
    await store.upsert(7, "tok", {"status": ST_READY})
    # zip entry order == page order; names carry no meaningful numbers
    _make_zip(store.zip_path(7, "tok"), ["x0.jpg", "a1.jpg", "zz.jpg"])

    assert store.page_count(7, "tok") == 3
    assert await store.get_page_bytes(7, "tok", 1) == b"page-0"
    assert await store.get_page_bytes(7, "tok", 3) == b"page-2"
    # out of range / zero / negative -> None
    assert await store.get_page_bytes(7, "tok", 4) is None
    assert await store.get_page_bytes(7, "tok", 0) is None
    # namelist cache warm path
    assert await store.get_page_bytes(7, "tok", 2) == b"page-1"


@pytest.mark.asyncio
async def test_page_read_not_ready(tmp_path):
    store = ArchiveStore(tmp_path)
    await store.upsert(7, "tok", {"status": "downloading"})
    _make_zip(store.zip_path(7, "tok"), ["a.jpg"])
    # zip exists but status != ready -> never served
    assert await store.get_page_bytes(7, "tok", 1) is None


@pytest.mark.asyncio
async def test_page_read_unreadable_zip(tmp_path):
    store = ArchiveStore(tmp_path)
    await store.upsert(7, "tok", {"status": ST_READY})
    store.zip_path(7, "tok").write_bytes(b"not a zip")
    assert await store.get_page_bytes(7, "tok", 1) is None
    assert store.page_count(7, "tok") is None


@pytest.mark.asyncio
async def test_stats(tmp_path):
    store = ArchiveStore(tmp_path)
    await store.upsert(1, "a", {"status": ST_READY})
    await store.upsert(2, "b", {"status": "downloading"})
    _make_zip(store.zip_path(1, "a"), ["p.jpg"])  # zip bytes counted

    st = store.stats()
    assert st["entries"] == 2
    assert st["ready"] == 1
    assert st["by_status"] == {"ready": 1, "downloading": 1}
    assert st["bytes"] > 0
