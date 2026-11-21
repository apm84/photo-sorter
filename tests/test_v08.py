"""v0.8: suggested albums (events found from dates, places, tags and named people; photos only)."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photosorter import albums, suggest
from photosorter.config import db_path
from photosorter.db import DB

BEACH = (30.39, -86.49)


class Lib:
    """A small synthetic index (no models needed): files, tags, people and faces."""

    def __init__(self, out: Path):
        self.out = out
        self.db = DB(db_path(out))
        self.n = 0
        self.people = {}

    def add(self, when: datetime, type="photo", gps=None, city="", place="", tags=(), people=(), media=None,
            date_source="exif"):
        self.n += 1
        rel = f"Photos/{when:%Y}/IMG_{self.n:04d}.jpg"
        p = self.out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"jpeg %d" % self.n)
        fid = self.db.execute(
            "INSERT INTO files(src_path, filename, ext, size, sha256, taken_at, date_source, lat, lon, city, place, "
            "type, media, dest_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"/src/{self.n}.jpg", f"IMG_{self.n:04d}.jpg", ".jpg", 10, f"{self.n:064x}", when.isoformat(),
             date_source, gps[0] if gps else None, gps[1] if gps else None, city, place, type, media, rel)).lastrowid
        for t in tags:
            self.db.execute("INSERT INTO tags(file_id, tag, score) VALUES(?,?,?)", (fid, t, 0.6))
        for name in people:
            pid = self.person(name)
            self.db.execute("INSERT INTO faces(file_id, person_id) VALUES(?,?)", (fid, pid))
        self.db.commit()
        return fid

    def person(self, name, hidden=0):
        if name not in self.people:
            self.people[name] = self.db.execute("INSERT INTO people(name, hidden) VALUES(?,?)",
                                                (name, hidden)).lastrowid
        return self.people[name]

    def run(self, start: datetime, n: int, step_min=20, **kw):
        return [self.add(start + timedelta(minutes=step_min * i), **kw) for i in range(n)]


@pytest.fixture
def lib(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    lib = Lib(out)
    yield lib
    lib.db.close()


def beach_day(lib):
    """Jul 12 2024: morning at the beach (8 photos) + evening at the same place (6) = one event, plus junk."""
    d = datetime(2024, 7, 12, 10, 0)
    ids = []
    for i in range(8):
        people = (["Alex"] if i < 4 else []) + (["Sam"] if i % 3 == 0 else [])
        ids.append(lib.add(d + timedelta(minutes=20 * i), gps=BEACH, city="Destin", place="Destin, Florida",
                           tags=["beach", "ocean"] if i < 6 else ["sunset"], people=people))
    ids += lib.run(datetime(2024, 7, 12, 18, 0), 6, gps=(30.391, -86.491), city="Destin", place="Destin, Florida")
    junk = [lib.add(d + timedelta(minutes=5 + 30 * i), type=t, gps=BEACH, city="Destin")
            for i, t in enumerate(("screenshot", "meme", "screenshot", "document", "graphic", "meme"))]
    junk.append(lib.add(d + timedelta(minutes=50), type="video", media="video", gps=BEACH, city="Destin"))
    return ids, junk


def test_clustering_splits_on_gaps_and_distance_and_merges_same_place_same_day(lib):
    ids, _ = beach_day(lib)
    # Aug 1: 7 photos 10 minutes apart, but the last 3 are 100+ km away: two runs of 4 and 3, both too small
    lib.run(datetime(2024, 8, 1, 9), 4, step_min=10, gps=(41.88, -87.63))
    lib.run(datetime(2024, 8, 1, 9, 40), 3, step_min=10, gps=(42.96, -87.63))
    # Sep 5: only 5 photos
    lib.run(datetime(2024, 9, 5, 12), 5)
    # Oct 10/11: 6 photos, then 6 more the next day (gap > 3 h): two events
    lib.run(datetime(2024, 10, 10, 12), 6)
    lib.run(datetime(2024, 10, 11, 12), 6)
    runs = suggest.cluster(suggest.eligible_photos(lib.db))
    got = [sorted(p["id"] for p in r) for r in runs]
    assert sorted(ids) in got                       # morning + evening at the same beach: merged
    assert len(runs) == 3 and all(len(r) >= suggest.MIN_PHOTOS for r in runs)
    assert all(r[0]["t"].date() != datetime(2024, 8, 1).date() for r in runs)


def test_screenshots_memes_documents_graphics_and_videos_are_never_suggested(lib):
    ids, junk = beach_day(lib)
    elig = {p["id"] for p in suggest.eligible_photos(lib.db)}
    assert set(ids) <= elig and not (set(junk) & elig)
    # photos dated only by the file time are left out too
    lib.run(datetime(2024, 11, 1, 12), 8, date_source="mtime")
    r = suggest.find(lib.db)
    assert r["added"] == 1
    items = {x[0] for x in lib.db.query("SELECT file_id FROM album_suggestion_items")}
    assert items == set(ids)
    # a cluster made only of screenshots/memes gives nothing
    lib.run(datetime(2025, 1, 1, 12), 10, type="screenshot")
    lib.run(datetime(2025, 1, 2, 12), 10, type="meme")
    assert suggest.find(lib.db)["added"] == 0


def test_names_use_real_tags_places_people_and_dates(lib):
    beach_day(lib)
    lib.person("Hidden Person", hidden=1)
    suggest.find(lib.db)
    (s,) = suggest.list_open(lib.db)
    assert s["name"] == "Beach day in Destin with Alex and Sam, Jul 12, 2024"
    assert s["count"] == 14 and s["place"] == "Destin, Florida"
    # no tags, no place, no people: a plain, honest name; several days: a range
    lib.run(datetime(2024, 10, 10, 22), 8, step_min=40)
    suggest.find(lib.db)
    names = [x["name"] for x in suggest.list_open(lib.db)]
    assert "Trip, Oct 10-11, 2024" in names
    from collections import Counter
    a = [{"t": datetime(2023, 12, 24, 18)}, {"t": datetime(2023, 12, 24, 21)}]
    assert suggest.name_for(a, Counter({"christmas tree": 2, "food": 2}), ["Ann", "Bo", "Cy", "Di"]) == \
        "Christmas with Ann, Bo, Cy and 1 other, Dec 24, 2023"
    assert suggest.name_for(a, Counter(), [], "Lakeside") == "Day in Lakeside, Dec 24, 2023"
    assert suggest.name_for(a, Counter({"beach": 1}), ["Ann", "Bo"]) == "Get-together with Ann and Bo, Dec 24, 2023"


def test_skips_events_already_in_albums_and_remembers_dismissed(lib):
    ids, _ = beach_day(lib)
    other = lib.run(datetime(2024, 10, 10, 12), 8)
    a = albums.create_album(lib.db, lib.out, "Beach")
    albums.add_files(lib.db, lib.out, a["id"], ids[:10])       # 10 of 14 already in an album
    r = suggest.find(lib.db)
    assert r["added"] == 1 and r["already_in_albums"] == 1
    (s,) = suggest.list_open(lib.db)
    suggest.dismiss(lib.db, s["id"])
    assert suggest.list_open(lib.db) == []
    lib.add(datetime(2024, 10, 10, 15, 0))                       # one more photo of that day: still not back
    r = suggest.find(lib.db)
    assert r["added"] == 0 and r["already_suggested"] == 1 and suggest.list_open(lib.db) == []
    assert len(other) == 8
    # running again never duplicates an open suggestion either
    lib.run(datetime(2024, 12, 1, 12), 7)
    assert suggest.find(lib.db)["added"] == 1 and suggest.find(lib.db)["added"] == 0


def test_suggestion_endpoints_review_keep_and_dismiss(lib):
    from fastapi.testclient import TestClient
    from photosorter.web.app import create_app
    ids, junk = beach_day(lib)
    second = lib.run(datetime(2024, 12, 1, 12), 7)
    c = TestClient(create_app(lib.out))
    h = {"X-PhotoSorter": "1"}
    assert c.post("/api/suggestions/find").status_code == 403
    r = c.post("/api/suggestions/find", headers=h).json()
    assert r["added"] == 2
    lst = c.get("/api/suggestions").json()["suggestions"]
    beach = next(s for s in lst if s["name"].startswith("Beach day"))
    snow = next(s for s in lst if s["id"] != beach["id"])
    # the review grid is the normal photo search, limited to the suggestion
    got = c.get(f"/api/search?suggestion={beach['id']}&ids_only=true").json()["ids"]
    assert sorted(got) == sorted(ids)
    rm = ids[:2]
    r = c.post(f"/api/suggestions/{beach['id']}/remove", json={"file_ids": rm}, headers=h).json()
    assert r["count"] == 12
    assert sorted(c.get(f"/api/search?suggestion={beach['id']}&ids_only=true").json()["ids"]) == sorted(ids[2:])
    assert c.post(f"/api/suggestions/{beach['id']}/rename", json={"name": "  Destin  2024 "}, headers=h).json()["name"] \
        == "Destin 2024"
    # Keep: a real album (hard links) with the photos left in the suggestion
    r = c.post(f"/api/suggestions/{beach['id']}/keep", json={}, headers=h)
    assert r.status_code == 200, r.text
    alb = r.json()["album"]
    assert alb["name"] == "Destin 2024" and r.json()["added"] == 12
    in_album = c.get(f"/api/search?album={alb['id']}&ids_only=true").json()["ids"]
    assert sorted(in_album) == sorted(ids[2:]) and not set(in_album) & set(junk)
    assert (lib.out / "Albums" / alb["folder"]).is_dir()
    assert c.get(f"/api/suggestions/{beach['id']}").json()["state"] == "kept"
    assert c.post(f"/api/suggestions/{beach['id']}/keep", json={}, headers=h).status_code == 409
    # Dismiss: gone, and Find doesn't bring it back
    assert c.post(f"/api/suggestions/{snow['id']}/dismiss", headers=h).json()["state"] == "dismissed"
    assert c.get("/api/suggestions").json()["suggestions"] == []
    assert c.post("/api/suggestions/find", headers=h).json()["added"] == 0
    assert c.get("/api/suggestions/99999").status_code == 404
    assert len(second) == 7
    # originals are never touched: the album links point at the sorted copies
    assert "Find suggestions" in c.get("/static/albums.js").text


def test_find_on_a_real_sorted_index(sample_src, tmp_path):
    """The pipeline's own index (types from SigLIP prompts / rules): find runs, and no screenshot is ever used."""
    from .test_v04 import run
    out = tmp_path / "out"
    run(sample_src, out)
    db = DB(db_path(out))
    try:
        r = suggest.find(db)
        assert r["added"] >= 0
        bad = db.query("SELECT f.id FROM album_suggestion_items i JOIN files f ON f.id=i.file_id "
                       "WHERE f.type<>'photo' OR f.media IS NOT NULL")
        assert bad == []
        assert all(p["id"] not in {x[0] for x in db.query("SELECT id FROM files WHERE type='screenshot'")}
                   for p in suggest.eligible_photos(db))
    finally:
        db.close()
