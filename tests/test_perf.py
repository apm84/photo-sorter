"""v0.3 performance: thumbnail cache (made once, 304s), keyset paging, gzip. Originals/sorted copies untouched."""
from __future__ import annotations

import gzip
import io
import os

from fastapi.testclient import TestClient
from PIL import Image

from photosorter import thumbs
from photosorter.config import Settings, db_path, index_dir
from photosorter.db import DB
from photosorter.pipeline import Pipeline
from photosorter.web.app import create_app

from .conftest import exif_bytes, scene, tree_hashes


def library_hashes(out):
    return {k: v for k, v in tree_hashes(out).items() if not k.startswith("_index")}


def test_thumbs_made_once_cached_and_304(sample_src, tmp_path, monkeypatch):
    # a sideways photo (EXIF orientation 6): the thumbnail must come out upright
    im = scene(42, 600, 400)
    ex = Image.Exif(); ex[0x0112] = 6; ex[0x0132] = "2021:05:05 10:00:00"; ex[0x9003] = "2021:05:05 10:00:00"
    im.save(sample_src / "rotated.jpg", exif=ex.tobytes(), quality=90)
    out = tmp_path / "out"
    src_before = tree_hashes(sample_src)
    Pipeline(Settings(sources=[str(sample_src)], output=str(out), use_ai=False, use_faces=False, workers=2)).run()
    lib_before = library_hashes(out)
    db = DB(db_path(out))
    vis = db.query("SELECT * FROM files WHERE analyzed=1 AND (is_keeper IS NULL OR is_keeper=1) "
                   "AND (rep_id IS NULL OR rep_id=id)")
    # pre-generated during the batch run, into _index/thumbs only
    for r in vis:
        p = thumbs.cache_path(out, r["id"])
        assert p.exists() and p.parent == index_dir(out) / "thumbs"
        assert max(Image.open(p).size) <= thumbs.THUMB_SIDE
    rot = next(r for r in vis if r["filename"] == "rotated.jpg")
    w, h = Image.open(thumbs.cache_path(out, rot["id"])).size
    assert h > w                                         # EXIF-rotated (portrait)

    made = []
    real = thumbs.open_image
    monkeypatch.setattr(thumbs, "open_image", lambda *a, **k: (made.append(a[0]), real(*a, **k))[1])
    c = TestClient(create_app(out, enable_ai=False))
    fid = vis[0]["id"]
    r1 = c.get(f"/thumb/{fid}.jpg?v={thumbs.version(vis[0])}")
    assert r1.status_code == 200 and r1.headers["content-type"] == thumbs.MEDIA_TYPE
    assert "immutable" in r1.headers["cache-control"] and "max-age=31536000" in r1.headers["cache-control"]
    assert made == []                                    # served from the cache, not regenerated
    tag = r1.headers["etag"]
    r2 = c.get(f"/thumb/{fid}.jpg?v={thumbs.version(vis[0])}", headers={"If-None-Match": tag})
    assert r2.status_code == 304 and r2.content == b"" and r2.headers["etag"] == tag
    assert "content-encoding" not in r1.headers          # images are never gzipped

    # preview (~1600 px) made on first request only, then cached
    p1 = c.get(f"/preview/{rot['id']}.jpg")
    p2 = c.get(f"/preview/{rot['id']}.jpg")
    assert p1.status_code == p2.status_code == 200 and len(made) == 1
    pw, ph = Image.open(io.BytesIO(p1.content)).size
    assert max(pw, ph) <= thumbs.PREVIEW_SIDE and ph > pw
    assert c.get(f"/preview/{rot['id']}.jpg", headers={"If-None-Match": p2.headers["etag"]}).status_code == 304

    # a missing or out-of-date cache file is rebuilt once
    thumbs.cache_path(out, fid).unlink()
    c.get(f"/thumb/{fid}.jpg"); c.get(f"/thumb/{fid}.jpg")
    assert len(made) == 2
    os.utime(thumbs.cache_path(out, fid), (1, 1))       # older than the photo
    c.get(f"/thumb/{fid}.jpg")
    assert len(made) == 3
    # HARD RULE: originals and the sorted copies are byte-for-byte unchanged
    assert tree_hashes(sample_src) == src_before
    assert library_hashes(out) == lib_before


def test_keyset_paging_and_gzip(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    for i in range(30):
        # pairs share a timestamp so ties on taken_at are exercised; a few have no date
        dt = None if i % 10 == 9 else f"20{10 + i // 6}:0{1 + i % 5}:01 12:00:00"
        scene(500 + i, 320, 240).save(src / f"p{i:02d}.jpg", exif=exif_bytes(dt, "Canon", "EOS") if dt else b"")
    out = tmp_path / "out"
    Pipeline(Settings(sources=[str(src)], output=str(out), use_ai=False, use_faces=False, workers=2)).run()
    c = TestClient(create_app(out, enable_ai=False))
    full = c.get("/api/search?limit=1000").json()
    want = [it["id"] for it in full["items"]]
    assert full["total"] == len(want) >= 25
    got, after, pages = [], "", 0
    while True:
        r = c.get("/api/search", params={"limit": 7, **({"after": after} if after else {})}).json()
        if pages == 0:
            assert r["total"] == len(want)
        else:
            assert r["total"] is None                    # only the first page is counted
        got += [it["id"] for it in r["items"]]; pages += 1
        if not r["next"]:
            break
        after = r["next"]
    assert got == want and pages == -(-len(want) // 7)
    offs = []
    for o in range(0, len(want), 7):
        offs += [it["id"] for it in c.get(f"/api/search?limit=7&offset={o}").json()["items"]]
    assert offs == want
    assert all("v" in it for it in full["items"])
    assert c.get("/api/search?after=nonsense").status_code == 400
    # JSON is gzipped
    r = c.get("/api/search?limit=1000", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"
    raw = c.stream("GET", "/api/search?limit=1000", headers={"Accept-Encoding": "gzip"})
    with raw as resp:
        body = b"".join(resp.iter_raw())
    assert len(body) < len(gzip.decompress(body)) / 2
    db = DB(db_path(out))
    idx = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"ix_files_taken_id", "ix_faces_person_file", "ix_album_items_file"} <= idx
