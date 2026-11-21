"""v0.9: album relevance / quality score and sort, Add to album from a suggestion, image sizing, sync waits."""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from PIL import Image

from photosorter import albums, albumscore, suggest, sync, thumbs
from photosorter.web.app import STATIC

from .test_v08 import BEACH, Lib, beach_day, lib  # noqa: F401  (fixture)


def _real_jpegs(lib, size=(2400, 1800)):
    for r in lib.db.query("SELECT dest_path FROM files"):
        Image.new("RGB", size, (30, 90, 160)).save(lib.out / r[0], "JPEG")
    lib.db.execute("UPDATE files SET width=?, height=?", size)
    lib.db.commit()


def test_score_prefers_a_tight_clean_event_and_explains_itself(lib):  # noqa: F811
    ids, junk = beach_day(lib)
    lib.db.execute("UPDATE files SET width=2400, height=1800")
    lib.db.commit()
    good = albumscore.score_files(lib.db, ids)
    keys = [p["key"] for p in good["parts"]]
    assert keys == ["size", "time", "place", "people", "quality", "clean"]      # no scene for user albums
    assert all(0 <= p["value"] <= 1 for p in good["parts"]) and good["text"].startswith(f"Score {good['score']}:")
    # the same number of photos spread over a year, in different cities, with screenshots and no people
    spread = []
    for i in range(10):
        spread.append(lib.add(datetime(2023, 1, 1) + timedelta(days=36 * i), gps=(30 + i * 2, -90 + i * 3)))
    spread += junk[:4]
    lib.db.execute(f"UPDATE files SET width=640, height=480 WHERE id IN ({','.join('?' * len(spread))})", spread)
    lib.db.commit()
    bad = albumscore.score_files(lib.db, spread)
    assert good["score"] > bad["score"] + 30
    parts = {p["key"]: p for p in bad["parts"]}
    assert parts["time"]["value"] == 0 and parts["clean"]["value"] < 0.75 and parts["people"]["value"] == 0
    # no GPS at all: 'one place' is left out instead of counting as 0
    nogps = lib.run(datetime(2024, 5, 5, 9), 8)
    assert "place" not in [p["key"] for p in albumscore.score_files(lib.db, nogps)["parts"]]
    assert albumscore.score_files(lib.db, [])["score"] == 0


def test_suggestion_score_includes_scene_confidence(lib):  # noqa: F811
    beach_day(lib)
    suggest.find(lib.db)
    (s,) = suggest.list_open(lib.db)
    sc = albumscore.score_suggestion(lib.db, s["id"])
    scene = next(p for p in sc["parts"] if p["key"] == "scene")
    assert scene["value"] == round(6 / 14, 3)          # 6 of the 14 photos carry the 'beach' tag


def test_albums_and_suggestions_api_scores_and_add_to_album_uses_hard_links(lib):  # noqa: F811
    from fastapi.testclient import TestClient
    from photosorter.web.app import create_app
    ids, _ = beach_day(lib)
    more = lib.run(datetime(2024, 12, 1, 12), 7)
    _real_jpegs(lib)
    small = albums.create_album(lib.db, lib.out, "Small")
    albums.add_files(lib.db, lib.out, small["id"], more[:2])
    big = albums.create_album(lib.db, lib.out, "Beach")
    albums.add_files(lib.db, lib.out, big["id"], ids)
    c = TestClient(create_app(lib.out))
    h = {"X-PhotoSorter": "1"}
    plain = c.get("/api/albums").json()["albums"]
    assert all("score" not in a for a in plain)                      # only computed when asked for
    scored = {a["name"]: a for a in c.get("/api/albums?scores=1").json()["albums"]}
    assert scored["Beach"]["score"] > scored["Small"]["score"] and "Score" in scored["Beach"]["score_text"]
    c.post("/api/suggestions/find", headers=h)
    sug = c.get("/api/suggestions?scores=1").json()["suggestions"]
    assert sug and all(isinstance(s["score"], int) and s["score_parts"] for s in sug)
    # suggestion review: Actions > Add to album... posts the selected photos to the normal album endpoint
    sid = next(s["id"] for s in sug if s["count"] == 7)
    picked = c.get(f"/api/search?suggestion={sid}&ids_only=true").json()["ids"][:3]
    r = c.post("/api/albums/add", json={"file_ids": picked, "name": "Picked from a suggestion"}, headers=h).json()
    assert r["added"] == 3 and r["linked"] == 3 and r["copied"] == 0
    for row in lib.db.query("SELECT i.link_path, f.dest_path FROM album_items i JOIN files f ON f.id=i.file_id "
                            "WHERE i.album_id=?", (r["album"],)):
        assert os.path.samefile(lib.out / row[0], lib.out / row[1])   # a hard link, not a second copy
    r = c.post("/api/albums/add", json={"file_ids": picked[:1], "album": small["id"]}, headers=h).json()
    assert r["added"] == 1
    js = c.get("/static/albums.js").text
    assert "Add to album..." in js and "Relevance / quality" in js and "Number of photos (high to low)" in js


def test_images_are_constrained_everywhere():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".viewer-stage{grid-template:minmax(0,1fr)/minmax(0,1fr)}" in css
    assert "img{max-width:100%}" in css and ".grid>*,.named>*" in css
    assert ".card-open img{width:100%;height:100%;aspect-ratio:1;object-fit:cover" in css


def test_sync_only_waits_for_the_user_when_a_preview_must_be_made(lib, tmp_path):  # noqa: F811
    fid = lib.add(datetime(2024, 1, 1, 12))
    _real_jpegs(lib)
    row = dict(lib.db.one("SELECT id, src_path, dest_path, mtime, size FROM files WHERE id=?", (fid,)))
    naps = []
    s = sync.Syncer(lib.out, cfg={**sync.DEFAULTS, "pi_url": "http://127.0.0.1:1"}, token="t" * 40,
                    client=sync.PiClient("http://127.0.0.1:1", "t" * 40), sleep=naps.append, idle_fn=lambda: 0.0)
    try:
        assert not thumbs.is_fresh(lib.out, row, "thumb")
        p = s._path_for({"kind": "t", "row": row})                 # must be made: waits for the busy user
        assert p and p.exists() and sum(naps) > 0
        naps.clear()
        assert thumbs.is_fresh(lib.out, row, "thumb")
        assert s._path_for({"kind": "t", "row": row}) == p and naps == []   # cached: sent at once
    finally:
        s.hc.close()
