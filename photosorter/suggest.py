"""Suggested albums (v0.8): group photos into events (outings, trips, gatherings) and name them.

Everything is local and read-only on the library: it only reads the index (dates, GPS/place, the image tags the
sort run already stored from the existing SigLIP 2 embeddings, and named people from faces). Nothing is
re-embedded, no model runs, and no photo is copied or moved until the user presses Keep (then the normal
album code makes hard links, as for any album).

Only real photos are used: files classified as screenshots, memes, documents or graphics are always left out,
as are videos, duplicates that aren't the kept copy, photos removed in Clean up and photos whose date is only a
file date. Events: photos sorted by time are split where there is more than GAP_HOURS between two photos or
where two consecutive geotagged photos are more than FAR_KM apart; then neighbouring runs on the same day at the
same place are merged. Runs smaller than MIN_PHOTOS are skipped, as are events mostly already in an album.

Suggestions the user dismissed or kept are remembered: a new run never brings back an event that overlaps one
of them by OVERLAP or more.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime

GAP_HOURS = 3.0
FAR_KM = 25.0
MERGE_HOURS = 8.0
NEAR_KM = 2.0
MIN_PHOTOS = 6
COVERED = 0.6            # an event with this share of its photos already in albums is not suggested
OVERLAP = 0.5            # ... nor one that overlaps an existing (open, kept or dismissed) suggestion this much
EXCLUDED_TYPES = ("screenshot", "meme", "document", "graphic")

SCHEMA = """
CREATE TABLE IF NOT EXISTS album_suggestions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    start_at TEXT, end_at TEXT,
    place TEXT,
    state TEXT NOT NULL DEFAULT 'open',   -- 'open', 'kept' (album_id set) or 'dismissed'
    album_id INTEGER,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS album_suggestion_items (
    suggestion_id INTEGER NOT NULL,
    file_id INTEGER NOT NULL,
    removed INTEGER DEFAULT 0,             -- 1 = the user took it out of the suggestion
    PRIMARY KEY (suggestion_id, file_id)
);
CREATE INDEX IF NOT EXISTS ix_sugg_items_file ON album_suggestion_items(file_id);
"""

# image tags (prompts.TAG_VOCAB) that describe an event, and how a suggestion's name starts
SCENES = {
    "wedding": "Wedding", "birthday cake": "Birthday", "christmas tree": "Christmas",
    "halloween costume": "Halloween", "graduation": "Graduation", "fireworks": "Fireworks",
    "concert": "Concert", "party": "Party", "beach": "Beach day", "ocean": "Day at the ocean",
    "lake": "Lake day", "river": "Day at the river", "pool": "Pool day", "camping": "Camping trip",
    "hiking": "Hike", "fishing": "Fishing trip", "mountains": "Mountain trip", "snow": "Snow day",
    "zoo": "Zoo trip", "museum": "Museum visit", "park": "Park outing", "farm": "Farm visit",
    "garden": "Garden", "sports": "Game day", "football": "Football game", "baseball": "Baseball game",
    "basketball": "Basketball game", "church": "Church", "school": "School", "restaurant": "Dinner out",
    "boat": "Boat day", "airplane": "Trip", "city skyline": "City trip", "forest": "Day in the woods",
    "sunset": "Sunset", "flowers": "Flowers",
}
# when several event tags are present, these win (they say more about the day)
PRIORITY = ("wedding", "birthday cake", "christmas tree", "halloween costume", "graduation", "fireworks",
            "concert", "camping", "fishing", "hiking", "zoo", "museum", "football", "baseball", "basketball")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def ensure_schema(db) -> None:
    with db.lock:
        db.conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dt(s: str):
    try:
        return datetime.fromisoformat(s[:19])
    except (TypeError, ValueError):
        return None


def km(a, b) -> float:
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def eligible_photos(db) -> list[dict]:
    """Real photos only (never screenshots, memes, documents, graphics or videos), oldest first."""
    rows = db.query(
        "SELECT f.id, f.taken_at, f.lat, f.lon, f.city, f.place, f.type FROM files f "
        "LEFT JOIN file_review fr ON fr.file_id=f.id "
        "WHERE f.type='photo' AND f.media IS NULL AND f.taken_at IS NOT NULL "
        "AND COALESCE(f.date_source, '') <> 'mtime' "
        "AND (f.is_keeper IS NULL OR f.is_keeper=1) AND (f.rep_id IS NULL OR f.rep_id=f.id) "
        "AND f.dest_path IS NOT NULL AND COALESCE(fr.state, '') <> 'remove' "
        "ORDER BY f.taken_at, f.id")
    out = []
    for r in rows:
        if r["type"] in EXCLUDED_TYPES:           # (the SQL already keeps only 'photo'; belt and braces)
            continue
        t = _dt(r["taken_at"])
        if t is None:
            continue
        gps = (r["lat"], r["lon"]) if r["lat"] is not None and r["lon"] is not None else None
        out.append({"id": r["id"], "t": t, "gps": gps, "city": r["city"] or "", "place": r["place"] or ""})
    return out


def _centroid(run):
    pts = [p["gps"] for p in run if p["gps"]]
    if not pts:
        return None
    return (sum(a for a, _ in pts) / len(pts), sum(b for _, b in pts) / len(pts))


def _main_city(run) -> str:
    c = Counter(p["city"] for p in run if p["city"])
    if not c:
        return ""
    city, n = c.most_common(1)[0]
    return city if n >= 0.5 * sum(c.values()) else ""


def cluster(photos: list[dict], gap_hours=GAP_HOURS, far_km=FAR_KM, merge_hours=MERGE_HOURS,
            near_km=NEAR_KM, min_photos=MIN_PHOTOS) -> list[list[dict]]:
    """Split by time gaps / big jumps in place, merge same-day same-place neighbours, drop small runs."""
    runs: list[list[dict]] = []
    last_gps = None
    for p in photos:
        if runs:
            prev = runs[-1][-1]
            gap = (p["t"] - prev["t"]).total_seconds() / 3600
            far = bool(p["gps"] and last_gps and km(p["gps"], last_gps) > far_km)
            if gap > gap_hours or far:
                runs.append([p])
            else:
                runs[-1].append(p)
        else:
            runs.append([p])
        if p["gps"]:
            last_gps = p["gps"]
    merged: list[list[dict]] = []
    for r in runs:
        if merged:
            m = merged[-1]
            same_day = m[-1]["t"].date() == r[0]["t"].date()
            gap = (r[0]["t"] - m[-1]["t"]).total_seconds() / 3600
            ca, cb = _centroid(m), _centroid(r)
            if ca and cb:
                same_place = km(ca, cb) <= near_km
            else:
                a, b = _main_city(m), _main_city(r)
                same_place = bool(a) and a == b
            if same_day and gap <= merge_hours and same_place:
                m.extend(r)
                continue
        merged.append(list(r))
    return [r for r in merged if len(r) >= min_photos]


def _fmt_date(a: datetime, b: datetime) -> str:
    if a.date() == b.date():
        return f"{MONTHS[a.month - 1]} {a.day}, {a.year}"
    if (a.year, a.month) == (b.year, b.month):
        return f"{MONTHS[a.month - 1]} {a.day}-{b.day}, {a.year}"
    if a.year == b.year:
        return f"{MONTHS[a.month - 1]} {a.day} - {MONTHS[b.month - 1]} {b.day}, {a.year}"
    return f"{MONTHS[a.month - 1]} {a.day}, {a.year} - {MONTHS[b.month - 1]} {b.day}, {b.year}"


def _join(names: list[str], extra: int = 0) -> str:
    if extra:
        return ", ".join(names) + f" and {extra} other{'s' if extra != 1 else ''}"
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def scene_for(tag_counts: Counter, n: int) -> str | None:
    """The event tag seen in enough of the photos (at least 2 and 25%), favouring the more telling ones."""
    need = max(2, math.ceil(0.25 * n))
    ok = [t for t, c in tag_counts.items() if t in SCENES and c >= need]
    if not ok:
        return None
    for t in PRIORITY:
        if t in ok:
            return t
    order = list(SCENES)                       # ties: the earlier (more specific) scene in SCENES wins
    return max(ok, key=lambda t: (tag_counts[t], -order.index(t)))


def name_for(run: list[dict], tag_counts: Counter, people: list[str], city: str = "") -> str:
    """e.g. 'Beach day in Destin with Alex and Sam, Jul 12, 2024' (only real tags, places and named people)."""
    a, b = run[0]["t"], run[-1]["t"]
    multi = a.date() != b.date()
    tag = scene_for(tag_counts, len(run))
    if tag:
        head = SCENES[tag] + (f" in {city}" if city else "")
    elif city:
        head = f"Trip to {city}" if multi else f"Day in {city}"
    elif len(people) >= 2:
        head = "Get-together"
    else:
        head = "Trip" if multi else "Outing"
    if people:
        shown = people[:3]
        head += " with " + _join(shown, len(people) - len(shown))
    return f"{head}, {_fmt_date(a, b)}"


def _people_in(db, ids: list[int]) -> list[str]:
    """Named, visible people in at least 2 of the photos (or the only photo-person pair if that's all)."""
    if not ids:
        return []
    cnt: Counter = Counter()
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for r in db.query(
                "SELECT p.name, COUNT(DISTINCT fa.file_id) AS n FROM faces fa JOIN people p ON p.id=fa.person_id "
                f"WHERE fa.file_id IN ({','.join('?' * len(chunk))}) AND p.name IS NOT NULL AND p.name<>'' "
                "AND COALESCE(p.hidden, 0)=0 GROUP BY p.id", chunk):
            cnt[r["name"]] += r["n"]
    need = 2 if len(ids) >= 4 else 1
    return [n for n, c in sorted(cnt.items(), key=lambda x: (-x[1], x[0].lower())) if c >= need]


def _tags_in(db, ids: list[int]) -> Counter:
    cnt: Counter = Counter()
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for r in db.query(f"SELECT tag, COUNT(DISTINCT file_id) AS n FROM tags WHERE file_id IN "
                          f"({','.join('?' * len(chunk))}) GROUP BY tag", chunk):
            cnt[r["tag"]] += r["n"]
    return cnt


def find(db) -> dict:
    """Make new suggestions (on demand, and after a sort run). Existing ones are left as they are."""
    ensure_schema(db)
    photos = eligible_photos(db)
    runs = cluster(photos)
    in_album = {r[0] for r in db.query("SELECT DISTINCT file_id FROM album_items")}
    owner: dict[int, set] = {}
    for r in db.query("SELECT suggestion_id, file_id FROM album_suggestion_items"):
        owner.setdefault(r[1], set()).add(r[0])
    added, skipped_album, skipped_known = [], 0, 0
    for run in runs:
        ids = [p["id"] for p in run]
        if sum(i in in_album for i in ids) >= COVERED * len(ids):
            skipped_album += 1
            continue
        hits: Counter = Counter()
        for i in ids:
            for s in owner.get(i, ()):
                hits[s] += 1
        if hits and max(hits.values()) >= OVERLAP * len(ids):
            skipped_known += 1
            continue
        city = _main_city(run)
        name = name_for(run, _tags_in(db, ids), _people_in(db, ids), city)
        place = Counter(p["place"] for p in run if p["place"]).most_common(1)
        with db.lock:
            sid = db.execute("INSERT INTO album_suggestions(name, start_at, end_at, place, state, created_at) "
                             "VALUES(?,?,?,?, 'open', ?)",
                             (name, run[0]["t"].isoformat(timespec="seconds"), run[-1]["t"].isoformat(timespec="seconds"),
                              place[0][0] if place else None, _now())).lastrowid
            db.executemany("INSERT OR IGNORE INTO album_suggestion_items(suggestion_id, file_id) VALUES(?,?)",
                           [(sid, i) for i in ids])
            db.commit()
        for i in ids:
            owner.setdefault(i, set()).add(sid)
        added.append(sid)
    return {"added": len(added), "ids": added, "events": len(runs), "already_in_albums": skipped_album,
            "already_suggested": skipped_known, "open": len(list_open(db))}


class SuggestionError(Exception):
    pass


def list_open(db) -> list[dict]:
    ensure_schema(db)
    rows = db.query(
        "SELECT s.id, s.name, s.start_at, s.end_at, s.place, "
        "(SELECT COUNT(*) FROM album_suggestion_items i WHERE i.suggestion_id=s.id AND i.removed=0) AS count, "
        "(SELECT i.file_id FROM album_suggestion_items i JOIN files f ON f.id=i.file_id "
        " WHERE i.suggestion_id=s.id AND i.removed=0 ORDER BY f.taken_at, f.id LIMIT 1) AS cover "
        "FROM album_suggestions s WHERE s.state='open' ORDER BY s.start_at DESC, s.id DESC")
    return [dict(r) for r in rows if r["count"]]


def get(db, sid: int) -> dict:
    ensure_schema(db)
    r = db.one("SELECT id, name, start_at, end_at, place, state, album_id FROM album_suggestions WHERE id=?", (sid,))
    if not r:
        raise SuggestionError("suggestion not found")
    d = dict(r)
    d["count"] = db.one("SELECT COUNT(*) FROM album_suggestion_items WHERE suggestion_id=? AND removed=0", (sid,))[0]
    return d


def _open(db, sid: int) -> dict:
    s = get(db, sid)
    if s["state"] != "open":
        raise SuggestionError("this suggestion was already kept or dismissed")
    return s


def remove_files(db, sid: int, file_ids) -> dict:
    _open(db, sid)
    ids = [int(x) for x in file_ids]
    with db.lock:
        db.executemany("UPDATE album_suggestion_items SET removed=1 WHERE suggestion_id=? AND file_id=?",
                       [(sid, i) for i in ids])
        db.commit()
    return {"removed": len(ids), **get(db, sid)}


def rename(db, sid: int, name: str) -> dict:
    _open(db, sid)
    name = " ".join((name or "").split())[:120]
    if not name:
        raise SuggestionError("type a name")
    with db.lock:
        db.execute("UPDATE album_suggestions SET name=? WHERE id=?", (name, sid))
        db.commit()
    return get(db, sid)


def dismiss(db, sid: int) -> dict:
    _open(db, sid)
    with db.lock:
        db.execute("UPDATE album_suggestions SET state='dismissed' WHERE id=?", (sid,))
        db.commit()
    return get(db, sid)


def keep(db, out, sid: int, name: str | None = None) -> dict:
    """Make a real album (hard links, like any album) from the photos left in the suggestion."""
    from . import albums
    s = _open(db, sid)
    name = " ".join((name or s["name"]).split())
    ids = [r[0] for r in db.query("SELECT i.file_id FROM album_suggestion_items i JOIN files f ON f.id=i.file_id "
                                  "WHERE i.suggestion_id=? AND i.removed=0 ORDER BY f.taken_at, f.id", (sid,))]
    if not ids:
        raise SuggestionError("no photos left in this suggestion")
    try:
        a = albums.create_album(db, out, name)
    except albums.AlbumError as e:
        raise SuggestionError(str(e))
    res = albums.add_files(db, out, a["id"], ids)
    with db.lock:
        db.execute("UPDATE album_suggestions SET state='kept', album_id=?, name=? WHERE id=?", (a["id"], a["name"], sid))
        db.commit()
    return {"album": a, "added": res["added"], "message": res["message"]}
