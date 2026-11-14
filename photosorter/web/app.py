"""Local-only query web app (FastAPI). Bind to 127.0.0.1 only."""
from __future__ import annotations

import io
import re
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from PIL import Image
from pydantic import BaseModel

from ..config import db_path, index_dir
from ..db import DB, blob_to_vec
from ..imaging import open_image
from .. import albums, cleanup, thumbs as thumbcache
from ..facegroup import METHODS, add_negative, quality_thresholds
from ..pipeline import build_people_folders, cluster_faces, face_cluster_params, refresh_people

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


class NameBody(BaseModel):
    name: str | None = None


class MergeBody(BaseModel):
    into: int


class DecisionBody(BaseModel):
    keep: list[int]
    keeper: int | None = None


class ConfirmBody(BaseModel):
    confirm: bool = False


class AssignBody(BaseModel):
    person: int | None = None      # existing person id
    name: str | None = None        # or a name (existing or new person)


class RegroupBody(BaseModel):
    eps: float | None = None
    min_samples: int | None = None
    method: str | None = None
    min_px: float | None = None       # face quality thresholds (optional)
    min_score: float | None = None
    min_blur: float | None = None


class ConfirmFaceBody(BaseModel):
    person: int
    yes: bool


class AlbumBody(BaseModel):
    name: str


class AlbumFilesBody(BaseModel):
    file_ids: list[int]
    album: int | None = None           # existing album, or
    name: str | None = None            # a new album's name


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BROWSER_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
IMMUTABLE = "public, max-age=31536000, immutable"
GZIP_PATHS = ("/api/", "/static/")
GZIP_PAGES = {"/", "/people", "/albums", "/cleanup", "/verify", "/sync"}


class SelectiveGZip:
    """gzip JSON, HTML, JS and CSS; never images (already compressed)."""

    def __init__(self, app):
        self.app = app
        self.gz = GZipMiddleware(app, minimum_size=800)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path.startswith(GZIP_PATHS) or path in GZIP_PAGES:
            await self.gz(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def cached_image(request: Request, p: Path, tag: str, media_type: str, cache_control: str):
    """Serve a file from the _index cache with an ETag; answer 304 when the browser already has it."""
    headers = {"ETag": tag, "Cache-Control": cache_control}
    inm = request.headers.get("if-none-match", "")
    if inm and (inm.strip() == "*" or tag in [t.strip().removeprefix("W/") for t in inm.split(",")]):
        return Response(status_code=304, headers=headers)
    return FileResponse(p, media_type=media_type, headers=headers)


VISIBLE = "(f.is_keeper IS NULL OR f.is_keeper=1) AND (f.rep_id IS NULL OR f.rep_id=f.id)"


def create_app(output_root: str | Path, enable_ai: bool = True) -> FastAPI:
    out = Path(output_root)
    if not db_path(out).exists():
        raise FileNotFoundError(f"No index found in {out}. Run the sorter first.")
    db = DB(db_path(out))
    thumbs = index_dir(out) / "thumbs"
    thumbs.mkdir(exist_ok=True)
    app = FastAPI(title="Photo Sorter", docs_url=None, redoc_url=None)
    state = {"clip": None, "clip_tried": False, "emb_count": -1, "ids": None, "mat": None, "text_cache": {}}
    lock = threading.Lock()
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    app.add_middleware(SelectiveGZip)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.client.host if request.client else ""
        if host not in LOCAL_HOSTS:
            return JSONResponse({"detail": "local access only"}, status_code=403)
        return await call_next(request)

    def require_local_header(x_photosorter: str | None):
        # Custom header forces a CORS preflight, so other websites can't trigger actions.
        if x_photosorter != "1":
            raise HTTPException(403, "missing X-PhotoSorter header")

    def get_clip():
        """Text encoder of whichever embedder built this index (SigLIP 2 by default)."""
        if not enable_ai:
            return None
        with lock:
            if not state["clip_tried"]:
                state["clip_tried"] = True
                name = db.get_meta("embedder")
                try:
                    from ..embedders import get_embedder

                    state["clip"] = get_embedder(name, use_gpu=False, text_only=True) if name else None
                except Exception:
                    state["clip"] = None
            return state["clip"]

    def embeddings():
        n = db.one("SELECT COUNT(*) FROM embeddings")[0]
        if n != state["emb_count"]:
            rows = db.query("SELECT file_id, vec FROM embeddings ORDER BY file_id")
            state["ids"] = np.array([r[0] for r in rows], dtype=np.int64)
            state["mat"] = np.stack([blob_to_vec(r[1]) for r in rows]) if rows else np.zeros((0, 1), np.float32)
            state["emb_count"] = n
        return state["ids"], state["mat"]

    def file_row(fid: int):
        r = db.one("SELECT * FROM files WHERE id=?", (fid,))
        if not r:
            raise HTTPException(404)
        return r

    def best_path(r) -> Path:
        if r["dest_path"] and (out / r["dest_path"]).exists():
            return out / r["dest_path"]
        return Path(r["src_path"])

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/people", response_class=HTMLResponse)
    def people_page():
        return (STATIC / "people.html").read_text(encoding="utf-8")

    @app.get("/cleanup", response_class=HTMLResponse)
    def cleanup_page():
        return (STATIC / "cleanup.html").read_text(encoding="utf-8")

    @app.get("/albums", response_class=HTMLResponse)
    def albums_page():
        return (STATIC / "albums.html").read_text(encoding="utf-8")

    def text_vec(clip, q: str):
        cache = state["text_cache"]
        key = q.strip().lower()
        if key not in cache:
            if len(cache) > 256:
                cache.clear()
            cache[key] = clip.encode_texts([q])[0]
        return cache[key]

    def person_by_name(q: str):
        q = q.strip()
        if not q:
            return None
        r = db.one("SELECT id FROM people WHERE name IS NOT NULL AND lower(name)=lower(?) ORDER BY face_count DESC", (q,))
        return r[0] if r else None

    @app.get("/api/facets")
    def facets():
        vis = "(is_keeper IS NULL OR is_keeper=1) AND (rep_id IS NULL OR rep_id=id)"
        q = lambda sql: [dict(r) for r in db.query(sql)]
        return {
            "types": q(f"SELECT type AS value, COUNT(*) AS n FROM files WHERE {vis} GROUP BY type ORDER BY n DESC"),
            "years": q(f"SELECT substr(taken_at,1,4) AS value, COUNT(*) AS n FROM files WHERE {vis} AND taken_at IS NOT NULL "
                       "GROUP BY value ORDER BY value DESC"),
            "places": q(f"SELECT place AS value, COUNT(*) AS n FROM files WHERE {vis} AND place IS NOT NULL AND place<>'' "
                        "GROUP BY place ORDER BY n DESC LIMIT 300"),
            "states": q(f"SELECT state AS value, COUNT(*) AS n FROM files WHERE {vis} AND state IS NOT NULL AND state<>'' "
                        "GROUP BY state ORDER BY n DESC"),
            "countries": q(f"SELECT country AS value, COUNT(*) AS n FROM files WHERE {vis} AND country IS NOT NULL "
                           "AND country<>'' GROUP BY country ORDER BY n DESC"),
            "tags": q("SELECT tag AS value, COUNT(*) AS n FROM tags GROUP BY tag ORDER BY n DESC"),
            "people": q("SELECT id AS value, COALESCE(NULLIF(name,''), 'Person ' || id) AS label, face_count AS n "
                        "FROM people WHERE hidden=0 ORDER BY (name IS NULL OR name=''), face_count DESC"),
            "albums": [{"value": a["id"], "label": a["name"], "n": a["count"]} for a in albums.list_albums(db)],
            "clip_search": get_clip() is not None,
            "total": db.one("SELECT COUNT(*) FROM files")[0],
        }

    @app.get("/api/search")
    def search(q: str = "", type: str = "", year: str = "", month: str = "", place: str = "", state_: str = Query("", alias="state"),
               country: str = "", person: int | None = None, tag: str = "", duplicates: bool = False,
               unknown_date: bool = False, album: int | None = None, date_from: str = "", date_to: str = "",
               ids_only: bool = False, limit: int = Query(120, le=1000), offset: int = 0, after: str = ""):
        """Photos matching the filters, newest first. Page with `after` (the `next` cursor of the previous page,
        keyset paging: fast at any depth) or `offset`. The total is only counted for the first page."""
        where, params = [], []
        if album is not None:
            where.append("EXISTS (SELECT 1 FROM album_items i WHERE i.album_id=? AND i.file_id=f.id) AND NOT EXISTS "
                         "(SELECT 1 FROM file_review fr WHERE fr.file_id=f.id AND fr.state='remove')")
            params.append(album)
        elif not duplicates:
            where.append(VISIBLE)
        else:
            where.append("f.dup_group IS NOT NULL")
        for col, val in (("f.type", type), ("f.place", place), ("f.state", state_), ("f.country", country)):
            if val:
                where.append(f"{col}=?"); params.append(val)
        if year:
            where.append("substr(f.taken_at,1,4)=?"); params.append(year)
        if month:
            where.append("substr(f.taken_at,6,2)=?"); params.append(month.zfill(2))
        for val, op in ((date_from, ">="), (date_to, "<")):
            if val:
                if not DATE_RE.match(val):
                    raise HTTPException(400, "dates must look like 2026-11-13")
                # photos with no date drop out as soon as a range is set (NULL never compares true)
                where.append(f"f.taken_at {op} " + ("?" if op == ">=" else "date(?, '+1 day')")); params.append(val)
        if unknown_date:
            where.append("f.taken_at IS NULL")
        if person is not None:
            where.append("EXISTS (SELECT 1 FROM faces fa WHERE fa.file_id=COALESCE(f.rep_id, f.id) AND fa.person_id=?)")
            params.append(person)
        if tag:
            where.append("EXISTS (SELECT 1 FROM tags t WHERE t.file_id=COALESCE(f.rep_id, f.id) AND t.tag=?)"); params.append(tag)
        q = q.strip()
        named = person_by_name(q) if q else None
        if named is not None:            # typed a person's name: show that person's photos
            where.append("EXISTS (SELECT 1 FROM faces fa WHERE fa.file_id=COALESCE(f.rep_id, f.id) AND fa.person_id=?)")
            params.append(named)
            q = ""
        named_album = None
        if q and named is None:
            r = db.one("SELECT id FROM albums WHERE lower(name)=lower(?)", (q,))
            if r:                        # typed an album's name: show the album
                named_album = r[0]
                where.append("EXISTS (SELECT 1 FROM album_items i WHERE i.album_id=? AND i.file_id=f.id)")
                params.append(named_album)
                q = ""
        clip = get_clip() if q else None
        if q and clip is None:
            like = f"%{q}%"
            where.append("(f.filename LIKE ? OR f.place LIKE ? OR EXISTS (SELECT 1 FROM tags t WHERE t.file_id=f.id AND t.tag LIKE ?) "
                         "OR EXISTS (SELECT 1 FROM faces fa JOIN people p ON p.id=fa.person_id "
                         "WHERE fa.file_id=COALESCE(f.rep_id, f.id) AND p.name LIKE ?) "
                         "OR EXISTS (SELECT 1 FROM album_items i JOIN albums a ON a.id=i.album_id "
                         "WHERE i.file_id=f.id AND a.name LIKE ?))")
            params += [like, like, like, like, like]
        sql = ("SELECT f.id, f.filename, f.taken_at, f.place, f.type, f.width, f.height, f.dest_path, f.src_path, "
               "f.dup_group, f.is_keeper, CAST(COALESCE(f.mtime, 0) AS INTEGER) || '-' || COALESCE(f.size, 0) AS v FROM files f")
        keyset = clip is None and not ids_only and not (duplicates and album is None)
        count_where, count_params = list(where), list(params)
        if keyset and after:
            t, _, last = after.rpartition("|")
            if not last.isdigit():
                raise HTTPException(400, "bad cursor")
            if t:
                where.append("(f.taken_at < ? OR (f.taken_at = ? AND f.id < ?) OR f.taken_at IS NULL)"); params += [t, t, int(last)]
            else:
                where.append("(f.taken_at IS NULL AND f.id < ?)"); params.append(int(last))
            offset = 0
        if where:
            sql += " WHERE " + " AND ".join(where)
        order = (" ORDER BY f.dup_group, f.taken_at DESC, f.id" if duplicates and album is None
                 else " ORDER BY (f.taken_at IS NULL), f.taken_at DESC, f.id DESC")
        if clip is not None:
            rows = db.query(sql, params)
            ids, mat = embeddings()
            pos = {int(i): k for k, i in enumerate(ids)}
            t = text_vec(clip, q)
            scored = [(float(mat[pos[r["id"]]] @ t), r) for r in rows if r["id"] in pos]
            scored.sort(key=lambda x: -x[0])
            total = len(scored)
            if ids_only:
                return {"total": total, "ids": [r["id"] for _, r in scored[:50000]]}
            page = scored[offset:offset + limit]
            items = [{**dict(r), "score": round(s, 4)} for s, r in page]
        else:
            if ids_only:
                rows = db.query(sql.replace("SELECT f.id, f.filename, f.taken_at, f.place, f.type, f.width, f.height, "
                                            "f.dest_path, f.src_path, f.dup_group, f.is_keeper", "SELECT f.id", 1)
                                + order + " LIMIT 50000", params)
                return {"total": len(rows), "ids": [r[0] for r in rows]}
            total = None
            if not (keyset and after):
                cnt = "SELECT COUNT(*) FROM files f" + (" WHERE " + " AND ".join(count_where) if count_where else "")
                total = db.one(cnt, count_params)[0]
            rows = db.query(sql + order + " LIMIT ? OFFSET ?", params + [limit, offset])
            items = [dict(r) for r in rows]
        nxt = None
        if keyset and len(items) == limit:
            nxt = f"{items[-1]['taken_at'] or ''}|{items[-1]['id']}"
        return {"total": total, "items": items, "next": nxt, "clip": clip is not None, "person": named,
                "album": named_album}

    @app.get("/api/suggest")
    def suggest(q: str = "", limit: int = Query(6, le=20)):
        """Autosuggest while typing: people names, places, tags that contain the text."""
        q = q.strip()
        if not q:
            return {"people": [], "places": [], "tags": []}
        like = f"%{q}%"
        pre = f"{q}%"
        people_ = [dict(r) for r in db.query(
            "SELECT id, name AS label, face_count AS n FROM people WHERE hidden=0 AND name IS NOT NULL AND name LIKE ? "
            "ORDER BY (name NOT LIKE ?), face_count DESC LIMIT ?", (like, pre, limit))]
        places = [dict(r) for r in db.query(
            "SELECT place AS value, COUNT(*) AS n FROM files WHERE place LIKE ? GROUP BY place "
            "ORDER BY (place NOT LIKE ?), n DESC LIMIT ?", (like, pre, limit))]
        tags = [dict(r) for r in db.query(
            "SELECT tag AS value, COUNT(*) AS n FROM tags WHERE tag LIKE ? GROUP BY tag "
            "ORDER BY (tag NOT LIKE ?), n DESC LIMIT ?", (like, pre, limit))]
        albums_ = [dict(r) for r in db.query(
            "SELECT a.id, a.name AS label, (SELECT COUNT(*) FROM album_items i WHERE i.album_id=a.id) AS n FROM albums a "
            "WHERE a.name LIKE ? ORDER BY (a.name NOT LIKE ?), lower(a.name) LIMIT ?", (like, pre, limit))]
        return {"people": people_, "places": places, "tags": tags, "albums": albums_}

    @app.get("/api/file/{fid}")
    def file_info(fid: int):
        r = dict(file_row(fid))
        r["tags"] = [dict(t) for t in db.query("SELECT tag, score FROM tags WHERE file_id=? ORDER BY score DESC", (fid,))]
        r["people"] = [dict(p) for p in db.query(
            "SELECT DISTINCT p.id, COALESCE(NULLIF(p.name,''), 'Person ' || p.id) AS label FROM faces fa "
            "JOIN people p ON p.id=fa.person_id WHERE fa.file_id=?", (r["rep_id"] or fid,))]
        return r

    def cached_thumb(request: Request, fid: int, kind: str):
        r = file_row(fid)
        try:
            p = thumbcache.ensure(out, r, kind)      # made once, then served from _index/thumbs
        except Exception:
            raise HTTPException(404, "unreadable")
        cc = IMMUTABLE if request.query_params.get("v") else "public, max-age=86400"
        return cached_image(request, p, thumbcache.etag(p, kind), thumbcache.MEDIA_TYPE, cc)

    # URLs keep the .jpg name from 0.2; the content is WebP when Pillow supports it (Content-Type says which)
    @app.get("/thumb/{fid}.jpg")
    def thumb(fid: int, request: Request):
        return cached_thumb(request, fid, "thumb")

    @app.get("/preview/{fid}.jpg")
    def preview(fid: int, request: Request):
        return cached_thumb(request, fid, "preview")

    @app.get("/full/{fid}")
    def full_size(fid: int):
        """The photo at full size (library copy, else the original, read-only). Formats a browser can't show
        (HEIC, TIFF) are converted to JPEG on the fly."""
        r = file_row(fid)
        p = best_path(r)
        if not p.exists():
            raise HTTPException(404, "file not found")
        if p.suffix.lower() in BROWSER_EXTS:
            return FileResponse(p, headers={"Cache-Control": "max-age=3600"})
        try:
            im = open_image(p)
        except Exception:
            raise HTTPException(404, "unreadable")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
        return Response(buf.getvalue(), media_type="image/jpeg")

    @app.get("/face/{face_id}.jpg")
    def face_crop(face_id: int, request: Request):
        fa = db.one("SELECT * FROM faces WHERE id=?", (face_id,))
        if not fa:
            raise HTTPException(404)
        fp = thumbs / "faces" / f"{face_id}.jpg"
        if fp.exists():
            st = fp.stat()
            return cached_image(request, fp, f'"face-{face_id}-{int(st.st_mtime)}-{st.st_size}"', "image/jpeg",
                                "public, max-age=86400")
        r = file_row(fa["file_id"])
        try:
            im = open_image(best_path(r), max_side=1600)
        except Exception:
            raise HTTPException(404)
        W, H = im.size
        x1, y1, x2, y2 = fa["x1"] * W, fa["y1"] * H, fa["x2"] * W, fa["y2"] * H
        pad = 0.25 * max(x2 - x1, y2 - y1)
        crop = im.crop((max(0, x1 - pad), max(0, y1 - pad), min(W, x2 + pad), min(H, y2 + pad)))
        crop.thumbnail((160, 160))
        buf = io.BytesIO()
        crop.save(buf, "JPEG", quality=85)
        fp.parent.mkdir(exist_ok=True)
        fp.write_bytes(buf.getvalue())
        return Response(buf.getvalue(), media_type="image/jpeg")

    @app.post("/api/open/{fid}")
    def open_location(fid: int, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        p = best_path(file_row(fid))
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(p)])
        return {"path": str(p)}

    @app.get("/api/people")
    def people(hidden: bool = False):
        res = []
        for p in db.query("SELECT * FROM people WHERE hidden=0 OR ? ORDER BY (name IS NULL OR name=''), face_count DESC",
                          (1 if hidden else 0,)):
            faces = [r[0] for r in db.query("SELECT id FROM faces WHERE person_id=? ORDER BY locked DESC, score DESC LIMIT 12",
                                            (p["id"],))]
            res.append({"id": p["id"], "name": p["name"], "face_count": p["face_count"], "faces": faces,
                        "hidden": p["hidden"]})
        return res

    @app.get("/api/people/named")
    def people_named():
        """Main People grid: named people only, with one face photo and a photo count."""
        res = []
        for p in db.query("SELECT id, name, face_count FROM people WHERE name IS NOT NULL AND name<>'' AND hidden=0 "
                          "ORDER BY lower(name)"):
            cover = db.one("SELECT id FROM faces WHERE person_id=? ORDER BY locked DESC, low_quality ASC, "
                           "COALESCE(face_px,0) * COALESCE(score,0) DESC LIMIT 1", (p["id"],))
            photos = db.one("SELECT COUNT(DISTINCT file_id) FROM faces WHERE person_id=?", (p["id"],))[0]
            res.append({"id": p["id"], "name": p["name"], "face_count": p["face_count"], "photos": photos,
                        "cover_face": cover[0] if cover else None})
        return res

    @app.get("/api/people/{pid}/info")
    def person_info(pid: int):
        p = db.one("SELECT id, name, face_count, hidden FROM people WHERE id=?", (pid,))
        if not p:
            raise HTTPException(404)
        years = [dict(r) for r in db.query(
            "SELECT substr(f.taken_at,1,4) AS value, COUNT(DISTINCT f.id) AS n FROM faces fa JOIN files f "
            "ON f.id=fa.file_id WHERE fa.person_id=? AND f.taken_at IS NOT NULL GROUP BY value ORDER BY value DESC", (pid,))]
        return {**dict(p), "years": years}

    @app.post("/api/people/{pid}/name")
    def name_person(pid: int, body: NameBody, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        name = (body.name or "").strip() or None
        db.execute("UPDATE people SET name=? WHERE id=?", (name, pid)); db.commit()
        return {"ok": True}

    @app.post("/api/people/{pid}/merge")
    def merge_person(pid: int, body: MergeBody, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        if body.into == pid:
            raise HTTPException(400)
        if not db.one("SELECT 1 FROM people WHERE id=?", (body.into,)):
            raise HTTPException(404)
        # merged faces are locked so re-grouping later never splits them apart again
        db.execute("UPDATE faces SET person_id=?, locked=1 WHERE person_id IN (?, ?)", (body.into, pid, body.into))
        # what the user said about the merged-away person now applies to the person it joined
        db.execute("UPDATE OR IGNORE face_negatives SET person_id=? WHERE person_id=?", (body.into, pid))
        db.execute("DELETE FROM face_negatives WHERE person_id=?", (pid,))
        db.execute("UPDATE faces SET not_person=? WHERE not_person=?", (body.into, pid))
        db.execute("UPDATE faces SET suggest_person=? WHERE suggest_person=?", (body.into, pid))
        tgt = db.one("SELECT name FROM people WHERE id=?", (body.into,))[0]
        src = db.one("SELECT name FROM people WHERE id=?", (pid,))
        if not tgt and src and src[0]:
            db.execute("UPDATE people SET name=? WHERE id=?", (src[0], body.into))
        db.execute("DELETE FROM people WHERE id=?", (pid,))
        refresh_people(db)
        return {"ok": True}

    @app.post("/api/people/{pid}/hide")
    def hide_person(pid: int, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        db.execute("UPDATE people SET hidden=1 WHERE id=?", (pid,)); db.commit()
        return {"ok": True}

    @app.post("/api/people/{pid}/unhide")
    def unhide_person(pid: int, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        db.execute("UPDATE people SET hidden=0 WHERE id=?", (pid,)); db.commit()
        return {"ok": True}

    @app.get("/api/people/{pid}/faces")
    def person_faces(pid: int, offset: int = 0, limit: int = Query(120, le=500)):
        rows = db.query("SELECT id, file_id, score, locked FROM faces WHERE person_id=? ORDER BY locked DESC, score DESC "
                        "LIMIT ? OFFSET ?", (pid, limit, offset))
        total = db.one("SELECT COUNT(*) FROM faces WHERE person_id=?", (pid,))[0]
        return {"total": total, "faces": [dict(r) for r in rows]}

    @app.get("/api/faces/unassigned")
    def unassigned_faces(offset: int = 0, limit: int = Query(60, le=500)):
        rows = db.query("SELECT id, file_id, score FROM faces WHERE person_id IS NULL ORDER BY score DESC LIMIT ? OFFSET ?",
                        (limit, offset))
        total = db.one("SELECT COUNT(*) FROM faces WHERE person_id IS NULL")[0]
        return {"total": total, "faces": [dict(r) for r in rows]}

    @app.post("/api/faces/{face_id}/remove")
    def remove_face(face_id: int, x_photosorter: str | None = Header(None)):
        """Wrong face in this person: take it out, and never let re-grouping put it back there."""
        require_local_header(x_photosorter)
        fa = db.one("SELECT person_id FROM faces WHERE id=?", (face_id,))
        if not fa:
            raise HTTPException(404)
        db.execute("UPDATE faces SET person_id=NULL, not_person=?, locked=1, suggest_person=NULL, suggest_sim=NULL "
                   "WHERE id=?", (fa[0], face_id))
        if fa[0] is not None:
            add_negative(db, face_id, fa[0])     # learns: faces like this are not that person
        refresh_people(db)
        return {"ok": True}

    @app.post("/api/faces/{face_id}/assign")
    def assign_face(face_id: int, body: AssignBody, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        if not db.one("SELECT 1 FROM faces WHERE id=?", (face_id,)):
            raise HTTPException(404)
        pid = body.person
        name = (body.name or "").strip()
        if pid is None and name:
            pid = person_by_name(name) or db.execute("INSERT INTO people(name) VALUES(?)", (name,)).lastrowid
        if pid is None or not db.one("SELECT 1 FROM people WHERE id=?", (pid,)):
            raise HTTPException(400, "pick a person or type a name")
        db.execute("UPDATE faces SET person_id=?, locked=1, not_person=NULL, suggest_person=NULL, suggest_sim=NULL "
                   "WHERE id=?", (pid, face_id))
        db.execute("DELETE FROM face_negatives WHERE face_id=? AND person_id=?", (face_id, pid))
        refresh_people(db)
        return {"ok": True, "person": pid}

    @app.get("/api/faces/borderline")
    def borderline_faces(limit: int = Query(30, le=200)):
        """'Is this <Name>?' queue: faces that look like a named person but not clearly enough to add on their own."""
        rows = db.query(
            "SELECT fa.id, fa.file_id, fa.suggest_person AS person, fa.suggest_sim AS sim, p.name FROM faces fa "
            "JOIN people p ON p.id=fa.suggest_person WHERE fa.person_id IS NULL AND p.name IS NOT NULL AND p.name<>'' "
            "ORDER BY fa.suggest_sim DESC LIMIT ?", (limit,))
        total = db.one("SELECT COUNT(*) FROM faces fa JOIN people p ON p.id=fa.suggest_person WHERE fa.person_id IS NULL "
                       "AND p.name IS NOT NULL AND p.name<>''")[0]
        return {"total": total, "faces": [dict(r) for r in rows]}

    @app.post("/api/faces/{face_id}/confirm")
    def confirm_face(face_id: int, body: ConfirmFaceBody, x_photosorter: str | None = Header(None)):
        """Yes: lock the face to the person (it becomes a reference face). No: a negative example for them."""
        require_local_header(x_photosorter)
        if not db.one("SELECT 1 FROM faces WHERE id=?", (face_id,)) or not db.one(
                "SELECT 1 FROM people WHERE id=?", (body.person,)):
            raise HTTPException(404)
        if body.yes:
            db.execute("UPDATE faces SET person_id=?, locked=1, not_person=NULL, suggest_person=NULL, suggest_sim=NULL "
                       "WHERE id=?", (body.person, face_id))
            db.execute("DELETE FROM face_negatives WHERE face_id=? AND person_id=?", (face_id, body.person))
        else:
            add_negative(db, face_id, body.person)
            db.execute("UPDATE faces SET suggest_person=NULL, suggest_sim=NULL WHERE id=?", (face_id,))
            db.execute("UPDATE faces SET person_id=NULL WHERE id=? AND person_id=? AND locked=0", (face_id, body.person))
        refresh_people(db)
        return {"ok": True}

    @app.get("/api/people/regroup")
    def regroup_params():
        eps, ms, method = face_cluster_params(db)
        return {"eps": eps, "min_samples": ms, "method": method, "methods": list(METHODS),
                "faces": db.one("SELECT COUNT(*) FROM faces")[0],
                "low_quality": db.one("SELECT COUNT(*) FROM faces WHERE low_quality=1")[0],
                "quality": quality_thresholds(db)}

    @app.post("/api/people/regroup")
    def regroup(body: RegroupBody, x_photosorter: str | None = Header(None)):
        """Re-run face grouping with a new strictness. Names, merges, locks and removed faces are kept."""
        require_local_header(x_photosorter)
        eps, ms, method = face_cluster_params(db)
        eps = body.eps if body.eps is not None else eps
        ms = body.min_samples if body.min_samples is not None else ms
        method = body.method or method
        if not (0.05 <= eps <= 1.0) or ms < 1 or method not in METHODS:
            raise HTTPException(400, "bad grouping settings")
        q = {k: v for k, v in (("min_px", body.min_px), ("min_score", body.min_score), ("min_blur", body.min_blur))
             if v is not None}
        if q:
            if not (0 <= q.get("min_px", 0) <= 400 and 0 <= q.get("min_score", 0) <= 1 and 0 <= q.get("min_blur", 0) <= 2000):
                raise HTTPException(400, "bad face quality settings")
            db.set_meta("face_quality", {**(db.get_meta("face_quality") or {}), **q})
        db.set_meta("face_cluster", {"eps": eps, "min_samples": ms, "method": method})
        st = cluster_faces(db, eps, ms, method)
        db.set_meta("clustered_face_count", db.one("SELECT COUNT(*) FROM faces")[0])
        return {"ok": True, "people": db.one("SELECT COUNT(*) FROM people")[0], **st}

    # ---- duplicate clean-up ------------------------------------------------------------------------------
    def cleanup_call(fn, *a):
        try:
            return fn(db, out, *a)
        except cleanup.CleanupError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/cleanup/summary")
    def cleanup_summary():
        return cleanup.summary(db)

    @app.get("/api/cleanup/preview-all")
    def cleanup_preview_all():
        return cleanup.preview_all(db)

    @app.get("/api/cleanup/groups")
    def cleanup_groups(filter: str = "pending"):
        if filter == "pending":
            ids = cleanup.pending_group_ids(db)
        else:
            ids = [r[0] for r in db.query("SELECT id FROM dup_groups ORDER BY id")]
        return {"ids": ids}

    @app.get("/api/cleanup/group/{gid}")
    def cleanup_group(gid: int):
        try:
            return cleanup.group_detail(db, gid, out)
        except cleanup.CleanupError as e:
            raise HTTPException(404, str(e))

    @app.post("/api/cleanup/group/{gid}")
    def cleanup_decide(gid: int, body: DecisionBody, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        return cleanup_call(cleanup.apply_decision, gid, body.keep, body.keeper)

    @app.post("/api/cleanup/group/{gid}/delete-all")
    def cleanup_delete_all(gid: int, body: ConfirmBody, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        if not body.confirm:
            raise HTTPException(400, "confirmation required")
        return cleanup_call(cleanup.delete_group, gid)

    @app.post("/api/cleanup/approve-all")
    def cleanup_approve_all(body: ConfirmBody, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        if not body.confirm:
            raise HTTPException(400, "confirmation required")
        return cleanup_call(cleanup.approve_all)

    @app.post("/api/cleanup/undo")
    def cleanup_undo(x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        return cleanup_call(cleanup.undo_last)

    @app.get("/api/cleanup/learning")
    def cleanup_learning():
        from .. import prefs

        return prefs.status(db)

    @app.post("/api/cleanup/learning/reset")
    def cleanup_learning_reset(body: ConfirmBody, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        if not body.confirm:
            raise HTTPException(400, "confirm required")
        from .. import prefs

        return {"forgot": prefs.reset(db)}

    @app.get("/api/cleanup/tasks")
    def cleanup_tasks():
        return cleanup.tasks(db, out)

    @app.post("/api/cleanup/empty-trash")
    def cleanup_empty_trash(body: ConfirmBody, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        if not body.confirm:
            raise HTTPException(400, "confirmation required")
        return cleanup_call(cleanup.empty_trash)

    def reveal(p: Path):
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(p)])

    @app.post("/api/cleanup/open-report/{name}")
    def cleanup_open_report(name: str, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        if name not in ("exact-duplicates", "unreadable"):
            raise HTTPException(404)
        p = index_dir(out) / "reports" / f"{name}.csv"
        if not p.exists():
            raise HTTPException(404, "report not written yet")
        reveal(p)
        return {"path": str(p)}

    @app.post("/api/open-recycle-bin")
    def open_recycle_bin(x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "shell:RecycleBinFolder"])
        return {"ok": True}

    # ---- albums ------------------------------------------------------------------------------------------
    def album_call(fn, *a):
        try:
            return fn(db, out, *a)
        except albums.AlbumError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/albums")
    def albums_list():
        return {"albums": albums.list_albums(db)}

    @app.post("/api/albums")
    def albums_create(body: AlbumBody, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        return album_call(albums.create_album, body.name)

    @app.get("/api/albums/{aid}")
    def albums_get(aid: int):
        try:
            a = albums.get_album(db, aid)
        except albums.AlbumError as e:
            raise HTTPException(404, str(e))
        a["count"] = next((x["count"] for x in albums.list_albums(db) if x["id"] == aid), 0)
        a["path"] = str(out / albums.ALBUMS_DIR / a["folder"])
        a["years"] = [dict(r) for r in db.query(
            "SELECT substr(f.taken_at,1,4) AS value, COUNT(*) AS n FROM album_items i JOIN files f ON f.id=i.file_id "
            "WHERE i.album_id=? AND f.taken_at IS NOT NULL GROUP BY value ORDER BY value DESC", (aid,))]
        return a

    @app.post("/api/albums/add")
    def albums_add(body: AlbumFilesBody, x_photosorter: str | None = Header(None)):
        """Add photos to an existing album (album) or a new one (name)."""
        require_local_header(x_photosorter)
        aid = body.album
        if aid is None:
            if not (body.name or "").strip():
                raise HTTPException(400, "pick an album or type a new album name")
            existing = db.one("SELECT id FROM albums WHERE lower(name)=lower(?)", (" ".join(body.name.split()),))
            aid = existing[0] if existing else album_call(albums.create_album, body.name)["id"]
        return album_call(albums.add_files, aid, body.file_ids)

    @app.post("/api/albums/{aid}/remove")
    def albums_remove(aid: int, body: AlbumFilesBody, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        return album_call(albums.remove_files, aid, body.file_ids)

    @app.post("/api/albums/{aid}/rename")
    def albums_rename(aid: int, body: AlbumBody, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        return album_call(albums.rename_album, aid, body.name)

    @app.post("/api/albums/{aid}/delete")
    def albums_delete(aid: int, body: ConfirmBody, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        if not body.confirm:
            raise HTTPException(400, "confirmation required")
        return album_call(albums.delete_album, aid)

    @app.post("/api/albums/{aid}/open")
    def albums_open(aid: int, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        a = albums_get(aid)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", a["path"]])
        return {"path": a["path"]}

    @app.post("/api/albums/sync")
    def albums_sync(x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        return albums.sync_folders(db, out)

    # ---- verify before deleting originals (v0.4, read-only over the originals) ----------------------------
    from .. import verify as verifymod

    vstate = {"job": None, "thread": None, "rescue": None}

    @app.get("/verify", response_class=HTMLResponse)
    def verify_page():
        return (STATIC / "verify.html").read_text(encoding="utf-8")

    def verify_running() -> bool:
        t = vstate["thread"]
        return bool(t and t.is_alive())

    @app.get("/api/verify/status")
    def verify_status():
        job = vstate["job"]
        return {"running": verify_running(), "progress": job.progress if job else None,
                "summary": verifymod.summary(out), "rescue": vstate["rescue"],
                "sources": job.sources if job else verifymod.default_sources(out, db)}

    @app.post("/api/verify/start")
    def verify_start(x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        if verify_running():
            raise HTTPException(409, "a check is already running")
        job = verifymod.Verifier(out)
        if not job.sources:
            raise HTTPException(409, "no original folders known for this library")

        def work():
            try:
                job.run()
            except Exception:
                pass
        vstate["job"] = job
        vstate["thread"] = threading.Thread(target=work, daemon=True, name="verify")
        vstate["thread"].start()
        return {"started": True, "sources": job.sources}

    @app.post("/api/verify/stop")
    def verify_stop(x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        if vstate["job"]:
            vstate["job"].stop.set()
        return {"ok": True}

    @app.get("/api/verify/results")
    def verify_results(bucket: str = "", offset: int = 0, limit: int = Query(200, le=1000)):
        if bucket and bucket not in verifymod.BUCKETS:
            raise HTTPException(400, "unknown bucket")
        return {"results": verifymod.results(out, bucket, offset, limit)}

    @app.get("/api/verify/csv")
    def verify_csv():
        p = verifymod.write_csv(out)
        return FileResponse(p, media_type="text/csv", filename="verify-originals.csv")

    @app.post("/api/verify/rescue")
    def verify_rescue(body: ConfirmBody, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        if not body.confirm:
            raise HTTPException(400, "confirm required")
        if verify_running():
            raise HTTPException(409, "wait for the check to finish")
        res = verifymod.rescue(out)
        vstate["rescue"] = res
        return res

    @app.post("/api/verify/open-rescued")
    def verify_open_rescued(x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        d = out / verifymod.RESCUE_DIR
        if sys.platform == "win32" and d.is_dir():
            subprocess.Popen(["explorer", str(d)])
        return {"path": str(d)}

    # ---- family sync to the Pi (v0.4) ---------------------------------------------------------------------
    from .. import sync as syncmod

    @app.get("/sync", response_class=HTMLResponse)
    def sync_page():
        return (STATIC / "sync.html").read_text(encoding="utf-8")

    @app.get("/api/sync/status")
    def sync_status():
        return syncmod.status(out)

    @app.post("/api/sync/config")
    def sync_config(body: dict, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        try:
            return syncmod.update_config(out, body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/sync/test")
    def sync_test(x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        return syncmod.test_connection(out)

    @app.post("/api/sync/now")
    def sync_now(x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        try:
            return syncmod.request_sync(out)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/sync/pause")
    def sync_pause(body: dict, x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        return syncmod.set_paused(out, bool(body.get("paused")))

    @app.post("/api/people/build-folders")
    def people_folders(x_photosorter: str | None = Header(None)):
        require_local_header(x_photosorter)
        return {"links": build_people_folders(out, db)}

    return app


def serve(output_root: str | Path, port: int = 8765, open_browser: bool = True, page: str = ""):
    import uvicorn

    app = create_app(output_root)
    if open_browser:
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}/{page}")).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", log_config=None)
