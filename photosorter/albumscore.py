"""Album relevance / quality score (v0.9): how confident we are that an album is a real, good event album.

Used by the Albums page "Relevance / quality" sort, for user albums and suggested albums alike. It only reads
the index (dates, GPS, types, duplicates, faces and image tags the sort already stored); nothing is recomputed.

Each signal is a number from 0 to 1 with a weight; the score is the weighted average of the signals that apply,
times 100 (so 0-100). A signal that can't be measured (e.g. no GPS at all) is left out instead of counting as 0.

  signal   weight  1.0 means                                        how
  size       20    plenty of photos                                 1 - exp(-photos / 12): 6 -> 0.39, 24 -> 0.86
  time       20    taken within one day                             span <= 24 h -> 1; then 1 - log10(days) / 2
                                                                    (10 days -> 0.5, 100 days -> 0); needs 2 dated
  place      15    all near one place                               share of geotagged photos within 25 km of the
                                                                    median point; needs 2 geotagged photos
  people     15    half or more show someone you named              min(1, share with a named person / 0.5)
  quality    20    large, sharp photos                              per photo: resolution (0.3 MP -> 0 ... 2 MP -> 1)
                                                                    x (0.5 + 0.5 x share of its faces that are sharp);
                                                                    photos of unknown size are left out
  clean      10    no screenshots, memes, documents or duplicates   1 - junk share - duplicate share
  scene      10    the scene in its name shows in most photos       suggested albums only: share of photos with the
                                                                    scene tag the name came from (0 if none)
"""
from __future__ import annotations

import math
from collections import Counter

WEIGHTS = {"size": 20, "time": 20, "place": 15, "people": 15, "quality": 20, "clean": 10, "scene": 10}
LABELS = {"size": "photos", "time": "time span", "place": "one place", "people": "named people",
          "quality": "image quality", "clean": "no junk or duplicates", "scene": "matches its scene"}
JUNK = ("screenshot", "meme", "document", "graphic")
PLACE_KM = 25.0


def _km(a, b) -> float:
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _median(v):
    v = sorted(v)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def _rows(db, ids: list[int]) -> tuple[list, dict]:
    rows, faces = [], {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        q = ",".join("?" * len(chunk))
        rows += db.query("SELECT id, taken_at, lat, lon, type, width, height, dup_group, is_keeper, rep_id "
                         f"FROM files WHERE id IN ({q})", chunk)
        for r in db.query("SELECT fa.file_id, fa.low_quality, (p.id IS NOT NULL AND p.name IS NOT NULL AND "
                          "p.name<>'' AND COALESCE(p.hidden, 0)=0) AS named FROM faces fa "
                          f"LEFT JOIN people p ON p.id=fa.person_id WHERE fa.file_id IN ({q})", chunk):
            faces.setdefault(r[0], []).append((bool(r[1]), bool(r[2])))
    return rows, faces


def score_files(db, ids, scene_share: float | None = None) -> dict:
    """{score 0-100, parts: [{key, label, value 0-1, weight, detail}], text} for a set of file ids."""
    from datetime import datetime
    ids = sorted({int(i) for i in ids})
    rows, faces = _rows(db, ids)
    n = len(rows)
    parts = []

    def add(key, value, detail):
        parts.append({"key": key, "label": LABELS[key], "value": round(max(0.0, min(1.0, value)), 3),
                      "weight": WEIGHTS[key], "detail": detail})
    if not n:
        return {"score": 0, "parts": [], "text": "no photos"}
    add("size", 1 - math.exp(-n / 12), f"{n} photo{'s' if n != 1 else ''}")
    times = []
    for r in rows:
        try:
            times.append(datetime.fromisoformat(str(r["taken_at"])[:19]))
        except (TypeError, ValueError):
            pass
    if len(times) >= 2:
        hours = (max(times) - min(times)).total_seconds() / 3600
        v = 1.0 if hours <= 24 else 1 - math.log10(hours / 24) / 2
        add("time", v, f"within {hours:.0f} h" if hours <= 48 else f"over {hours / 24:.0f} days")
    pts = [(r["lat"], r["lon"]) for r in rows if r["lat"] is not None and r["lon"] is not None]
    if len(pts) >= 2:
        mid = (_median([a for a, _ in pts]), _median([b for _, b in pts]))
        near = sum(_km(p, mid) <= PLACE_KM for p in pts) / len(pts)
        add("place", near, f"{near:.0%} of geotagged photos near one place")
    named = sum(any(nm for _, nm in faces.get(r["id"], ())) for r in rows) / n
    add("people", named / 0.5, f"{named:.0%} show a named person")
    q = []
    for r in rows:
        if not (r["width"] and r["height"]):
            continue                              # size unknown: this photo doesn't count for quality
        mp = r["width"] * r["height"] / 1e6
        res = min(1.0, max(0.0, (mp - 0.3) / 1.7))
        fs = faces.get(r["id"])
        sharp = 1.0 if not fs else sum(not low for low, _ in fs) / len(fs)
        q.append(res * (0.5 + 0.5 * sharp))
    if q:
        add("quality", sum(q) / len(q), f"average {sum(q) / len(q):.0%}")
    junk = sum(r["type"] in JUNK for r in rows) / n
    dups = sum((r["rep_id"] is not None and r["rep_id"] != r["id"]) or
               (r["dup_group"] is not None and r["is_keeper"] == 0) for r in rows) / n
    add("clean", 1 - junk - dups, f"{junk:.0%} screenshots/memes/documents, {dups:.0%} duplicates")
    if scene_share is not None:
        add("scene", scene_share, f"{scene_share:.0%} of photos show the scene in its name")
    tw = sum(p["weight"] for p in parts)
    score = round(100 * sum(p["value"] * p["weight"] for p in parts) / tw)
    text = f"Score {score}: " + "; ".join(f"{p['label']} {round(p['value'] * 100)} ({p['detail']})" for p in parts)
    return {"score": score, "parts": parts, "text": text}


def album_ids(db, album_id: int) -> list[int]:
    from .albums import VISIBLE_ITEM
    return [r[0] for r in db.query(f"SELECT i.file_id FROM album_items i WHERE i.album_id=? AND {VISIBLE_ITEM}",
                                   (album_id,))]


def score_album(db, album_id: int) -> dict:
    return score_files(db, album_ids(db, album_id))


def score_suggestion(db, sid: int) -> dict:
    from . import suggest
    ids = [r[0] for r in db.query("SELECT file_id FROM album_suggestion_items WHERE suggestion_id=? AND removed=0",
                                  (sid,))]
    tags = suggest._tags_in(db, ids) if ids else Counter()
    scene = suggest.scene_for(tags, len(ids)) if ids else None
    share = (tags[scene] / len(ids)) if scene else 0.0
    return score_files(db, ids, scene_share=min(1.0, share))
