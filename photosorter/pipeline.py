"""The resumable processing pipeline. All state lives in the SQLite index in <output>/_index/."""
from __future__ import annotations

import csv
import json
import re
import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from . import background
from . import classify as cls
from . import video
from .config import IMAGE_EXTS, INDEX_DIRNAME, VIDEO_EXTS, Settings, db_path, index_dir
from .db import DB, blob_to_vec, to_signed64, vec_to_blob
from .dedupe import UnionFind, near_pairs, pick_keeper
from .facegroup import (DETECT_MAX_SIDE, backfill_face_px, blur_score, cluster_faces,  # noqa: F401 (re-exported)
                        face_cluster_params, refresh_people)
from .geo import reverse_geocode, safe_name
from .imaging import analyze_file, date_from_filename, open_image, sniff_kind, unreadable_reason

log = logging.getLogger("photosorter")

STAGES = ["scan", "analyze", "geocode", "embed", "classify", "faces", "people", "dedupe", "organize", "thumbs",
          "videos"]
STAGE_LABELS = {
    "scan": "Scan folders", "analyze": "Hash + EXIF", "geocode": "Places (offline)", "embed": "Image AI",
    "classify": "Type + tags", "faces": "Faces", "people": "Group people", "dedupe": "Duplicates",
    "organize": "Copy into tree", "thumbs": "Thumbnails", "videos": "Video previews",
}
TYPE_FOLDERS = {"screenshot": "Screenshots", "meme": "Memes", "document": "Documents", "graphic": "Graphics"}
SKIP_DIRS = {"$recycle.bin", "system volume information", INDEX_DIRNAME.lower(), ".thumbnails", "@eadir"}


_COPY_RE = re.compile(r"(\bcopy\b|kopie|copie|\(\d+\)|~\d+$|-\d+$|_\d$)", re.I)
_BACKUPISH = re.compile(r"(backup|copy|old|dup|temp|download)", re.I)


def copy_name_penalty(src_path: str, filename: str):
    """Lower is better: prefer 'IMG_0001.jpg' over 'IMG_0001 copy.jpg' / 'IMG_0001 (1).jpg' / backup folders."""
    stem = os.path.splitext(filename)[0]
    return (1 if _COPY_RE.search(stem) else 0, 1 if _BACKUPISH.search(os.path.dirname(src_path)) else 0,
            len(filename), len(src_path), src_path)


class Stopped(Exception):
    pass


class Pipeline:
    def __init__(self, settings: Settings, on_event: Callable[[dict], None] | None = None):
        self.s = settings
        self.on_event = on_event or (lambda e: None)
        self._pause = threading.Event()
        self._stop = threading.Event()
        self.out = Path(settings.output)
        self.out.mkdir(parents=True, exist_ok=True)
        self.db = DB(db_path(self.out))
        self.db.set_meta("settings", settings.to_dict())
        self._ai = None
        self._faces = None
        self.workers = background.worker_count(settings)
        self.gate = background.IdleGate(settings.run_mode, settings.idle_resume_s, stop=self._stop)
        # v0.6: videos need ffmpeg/ffprobe (bundled with the installer); without them they are left alone
        self.videos_on = bool(getattr(settings, "use_videos", True)) and video.available()
        self._check_model_changes()

    def _check_model_changes(self):
        """Switching embedder or face backend invalidates stored vectors (different spaces)."""
        if self.s.use_ai and self.db.get_meta("embedder") not in (None, self.s.embedder):
            self.db.execute("DELETE FROM embeddings")
            self.db.execute("UPDATE files SET embedded=0, classified=0")
            self.db.commit()
        if self.s.use_faces and self.db.get_meta("face_backend") not in (None, self.s.face_pack):
            self.db.execute("DELETE FROM faces")
            self.db.execute("DELETE FROM people WHERE name IS NULL OR name=''")
            self.db.execute("UPDATE people SET centroid=NULL, face_count=0")
            self.db.execute("UPDATE files SET faced=0")
            self.db.set_meta("clustered_face_count", None)
            self.db.commit()

    # ---- control -----------------------------------------------------------
    def pause(self):
        self._pause.set(); self.emit("paused")

    def resume(self):
        self._pause.clear(); self.emit("resumed")

    def stop(self):
        self._stop.set(); self._pause.clear()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def checkpoint(self):
        while self._pause.is_set() and not self._stop.is_set():
            time.sleep(0.2)
        if self._stop.is_set():
            raise Stopped()

    def emit(self, kind: str, **kw):
        self.on_event({"kind": kind, **kw})

    def info(self, msg: str, *a):
        text = msg % a if a else msg
        log.info(text)
        self.emit("log", text=text)

    def progress(self, stage: str, done: int, total: int, **extra):
        self._heartbeat()
        self.emit("progress", stage=stage, done=done, total=total, **extra)

    def _heartbeat(self, force: bool = False, clear: bool = False):
        """Tells the web app's Clean up page that a batch is running (it waits rather than moving files)."""
        now = time.time()
        if clear:
            self.db.set_meta("pipeline_heartbeat", None)
        elif force or now - getattr(self, "_hb", 0) > 10:
            self._hb = now
            self.db.set_meta("pipeline_heartbeat", now)

    # ---- run ---------------------------------------------------------------
    def run(self, stages: list[str] | None = None) -> dict:
        started = time.time()
        bg = background.apply(self.s)
        self.workers = background.worker_count(self.s)
        self.info("Speed: %s (priority %s, %d worker threads)", background.MODE_LABELS[bg["mode"]], bg["priority"],
                  self.workers)
        self._heartbeat(force=True)
        try:
            for st in stages or STAGES:
                self.checkpoint()
                self.emit("stage", stage=st, label=STAGE_LABELS[st])
                getattr(self, f"stage_{st}")()
            self.info("Finished in %.1fs", time.time() - started)
            summary = self.summary()
            self.emit("done", summary=summary)
            return summary
        except Stopped:
            self.db.commit()
            self.info("Stopped. Progress is saved; press Start to resume.")
            self.emit("stopped")
            return self.summary()
        except Exception as e:
            log.exception("Pipeline error")
            self.emit("error", text=str(e))
            raise
        finally:
            try:
                self._heartbeat(clear=True)
            except Exception:
                pass

    def summary(self) -> dict:
        q = lambda sql: self.db.one(sql)[0]
        types = {r[0] or "unknown": r[1] for r in self.db.query("SELECT type, COUNT(*) FROM files GROUP BY type")}
        return {
            "files": q("SELECT COUNT(*) FROM files"),
            "errors": q("SELECT COUNT(*) FROM files WHERE analyzed=-1"),
            "exact_duplicates": q("SELECT COUNT(*) FROM files WHERE rep_id IS NOT NULL AND rep_id<>id"),
            "dup_groups": q("SELECT COUNT(*) FROM dup_groups"),
            "with_gps": q("SELECT COUNT(*) FROM files WHERE lat IS NOT NULL"),
            "faces": q("SELECT COUNT(*) FROM faces"),
            "people": q("SELECT COUNT(*) FROM people"),
            "copied": q("SELECT COUNT(*) FROM files WHERE dest_path IS NOT NULL"),
            "videos": q("SELECT COUNT(*) FROM files WHERE media='video'"),
            "types": types,
        }

    # ---- stages ------------------------------------------------------------
    def stage_scan(self):
        known = {r["src_path"]: (r["size"], r["mtime"]) for r in self.db.query("SELECT src_path, size, mtime FROM files")}
        out_resolved = self.out.resolve()
        found = new = changed = nvid = 0
        batch = []
        exts = IMAGE_EXTS | VIDEO_EXTS if self.videos_on else IMAGE_EXTS
        if getattr(self.s, "use_videos", True) and not self.videos_on:
            self.info("Videos: skipped (ffmpeg/ffprobe not found; the installer includes them)")
        for root in self.s.sources:
            root_p = Path(root)
            if not root_p.is_dir():
                self.info("Source folder not found: %s", root)
                continue
            for dirpath, dirnames, filenames in os.walk(root_p):
                self.checkpoint()
                dp = Path(dirpath)
                try:
                    if dp.resolve() == out_resolved or out_resolved in dp.resolve().parents:
                        dirnames[:] = []
                        continue
                except OSError:
                    pass
                dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")]
                for fn in filenames:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext not in exts or fn.startswith("._"):
                        continue
                    nvid += ext in VIDEO_EXTS
                    p = str(dp / fn)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    found += 1
                    if p in known:
                        size, mtime = known[p]
                        if size == st.st_size and abs((mtime or 0) - st.st_mtime) < 1:
                            continue
                        changed += 1
                        self.db.execute(
                            "UPDATE files SET size=?, mtime=?, analyzed=0, geocoded=0, embedded=0, classified=0, "
                            "faced=0, organized=0, dest_path=NULL, error=NULL, rep_id=NULL WHERE src_path=?", (st.st_size, st.st_mtime, p))
                    else:
                        new += 1
                        batch.append((p, str(root_p), fn, ext, st.st_size, st.st_mtime))
                    if len(batch) >= 500:
                        self._insert_scanned(batch); batch = []
                    if found % 200 == 0:
                        self.progress("scan", found, 0)
        self._insert_scanned(batch)
        self.db.commit()
        self.progress("scan", found, found)
        self.info("Scan: %d images found (%d new, %d changed)", found - nvid, new, changed)
        if nvid:
            self.info("Scan: %d videos found (counted separately from images)", nvid)

    def _insert_scanned(self, batch):
        if batch:
            self.db.executemany(
                "INSERT OR IGNORE INTO files(src_path, src_root, filename, ext, size, mtime) VALUES(?,?,?,?,?,?)", batch)
            self.db.commit()

    def stage_analyze(self):
        from . import __version__
        from .imaging import heic_support

        heif = heic_support()
        if self.db.get_meta("unreadable_retry_version") != __version__:
            # files that failed with an older version get one more try (e.g. HEIC support added since)
            n = self.db.execute("UPDATE files SET analyzed=0 WHERE analyzed=-1").rowcount
            self.db.set_meta("unreadable_retry_version", __version__)
            if n:
                self.info("Analyze: retrying %d files that could not be read before", n)
        rows = self.db.query("SELECT id, src_path, filename, mtime FROM files WHERE analyzed=0 ORDER BY id")
        if not self.videos_on:      # videos indexed while ffmpeg was there wait until it is back
            rows = [r for r in rows if not video.is_video_path(r["src_path"])]
        if rows:
            self.info("HEIC/HEIF support: %s", f"on (pillow-heif {heif})" if heif else
                      "OFF (pillow-heif not installed; run Setup-PhotoSorter.bat)")
        total = len(rows)
        done = errors = 0

        def work(r):
            p = Path(r["src_path"])
            try:
                if self.videos_on and video.is_video_path(p):
                    return r, video.analyze_video(p), None
                return r, analyze_file(p), None
            except Exception as e:  # unreadable / corrupt
                if self.videos_on and not video.is_video_path(p) and sniff_kind(p) == "video":
                    try:                # v0.6: a video saved with a photo extension is sorted as a video
                        return r, video.analyze_video(p), None
                    except Exception:
                        pass
                return r, None, f"{type(e).__name__}: {e}"

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            for i in range(0, total, 64):
                self.checkpoint()
                for r, res, err in ex.map(work, rows[i:i + 64]):
                    if err:
                        errors += 1
                        self.db.execute("UPDATE files SET analyzed=-1, error=? WHERE id=?", (err[:500], r["id"]))
                        log.warning("Unreadable: %s (%s)", r["src_path"], unreadable_reason(Path(r["src_path"]), err))
                        continue
                    is_vid = res.get("media") == "video"
                    dt, src = res["taken_at"], ("metadata" if is_vid else "exif")
                    if dt is None:
                        dt, src = date_from_filename(r["filename"]), "filename"
                    if dt is None and (self.s.mtime_date_fallback or is_vid):
                        dt, src = datetime.fromtimestamp(r["mtime"]), "mtime"   # videos: always fall back
                    if dt is None:
                        src = None
                    self.db.execute(
                        "UPDATE files SET size=?, mtime=?, sha256=?, width=?, height=?, format=?, taken_at=?, date_source=?, "
                        "camera_make=?, camera_model=?, software=?, lat=?, lon=?, phash=?, dhash=?, analyzed=1, error=NULL, "
                        "media=?, duration=?, vcodec=?, acodec=?, playable=?, "
                        "organized=CASE WHEN dest_path LIKE 'Other%' THEN 0 ELSE organized END WHERE id=?",
                        (res["size"], res["mtime"], res["sha256"], res["width"], res["height"], res["format"],
                         dt.isoformat(timespec="seconds") if dt else None, src, res["camera_make"], res["camera_model"],
                         res["software"], res["lat"], res["lon"],
                         None if res["phash"] is None else to_signed64(res["phash"]),
                         None if res["dhash"] is None else to_signed64(res["dhash"]),
                         "video" if is_vid else None, res.get("duration"), res.get("vcodec"), res.get("acodec"),
                         (1 if res.get("playable") else 0) if is_vid else None, r["id"]))
                    if is_vid:
                        self._reset_video_cache(r["id"], res)
                    done += 1
                self.db.commit()
                self.progress("analyze", done + errors, total, errors=errors)
        self._assign_reps()
        self._mark_live_photos()
        self.progress("analyze", total, total, errors=errors)
        self.info("Analyze: %d files read, %d unreadable", done, errors)
        n, kinds = write_unreadable_report(self.db, self.out)
        if n:
            self.info("Unreadable files: %d in total (%s); list: %s", n,
                      ", ".join(f"{c} {k}" for k, c in kinds.most_common(4)),
                      str(index_dir(self.out) / "reports" / "unreadable.csv"))

    def _reset_video_cache(self, fid: int, res: dict):
        """A (re)analyzed video: fresh signature, and its old sprite/proxy are dropped (made again later)."""
        self.db.execute("INSERT OR REPLACE INTO videos(file_id, sig, hdr) VALUES(?,?,?)",
                        (fid, json.dumps(res.get("sig") or []), 1 if res.get("hdr") else 0))
        for p in (video.sprite_path(self.out, fid), video.proxy_path(self.out, fid)):
            p.unlink(missing_ok=True)

    def _mark_live_photos(self):
        """iPhone Live Photos come as IMG_1234.HEIC + IMG_1234.MOV (a ~3 s clip): mark those clips so the Videos
        tab can hide them. They are still copied and searchable like any video."""
        vids = self.db.query("SELECT id, src_path, duration FROM files WHERE media='video' AND analyzed=1")
        if not vids:
            return
        stems = {os.path.splitext(r[0])[0].lower() for r in self.db.query(
            "SELECT src_path FROM files WHERE media IS NULL AND analyzed=1")}
        upd = [(1 if (v["duration"] or 99) <= 4.5 and os.path.splitext(v["src_path"])[0].lower() in stems else 0,
                v["id"]) for v in vids]
        self.db.executemany("UPDATE files SET live=? WHERE id=?", upd)
        self.db.commit()

    def _assign_reps(self):
        """One representative per identical-content group (AI, faces and the tree use it). Stable once set."""
        for r in self.db.query("SELECT sha256 FROM files WHERE analyzed=1 AND rep_id IS NULL GROUP BY sha256"):
            members = self.db.query("SELECT id, src_path, filename, rep_id FROM files WHERE sha256=? AND analyzed=1",
                                    (r["sha256"],))
            existing = [m["rep_id"] for m in members if m["rep_id"]]
            rep = existing[0] if existing else min(members, key=lambda m: copy_name_penalty(m["src_path"], m["filename"]))["id"]
            self.db.execute("UPDATE files SET rep_id=? WHERE sha256=? AND rep_id IS NULL", (rep, r["sha256"]))
        self.db.commit()

    def stage_geocode(self):
        rows = self.db.query("SELECT id, lat, lon FROM files WHERE analyzed=1 AND geocoded=0 AND lat IS NOT NULL")
        self.db.execute("UPDATE files SET geocoded=1 WHERE analyzed=1 AND geocoded=0 AND lat IS NULL")
        total = len(rows)
        if total:
            self.info("Places: looking up %d GPS points offline...", total)
        for i in range(0, total, 2000):
            self.checkpoint()
            chunk = rows[i:i + 2000]
            res = reverse_geocode([(r["lat"], r["lon"]) for r in chunk])
            self.db.executemany(
                "UPDATE files SET cc=?, country=?, state=?, county=?, city=?, place=?, geocoded=1 WHERE id=?",
                [(g["cc"], g["country"], g["state"], g["county"], g["city"], g["place"], r["id"]) for r, g in zip(chunk, res)])
            self.db.commit()
            self.progress("geocode", min(i + 2000, total), total)
        self.progress("geocode", total, total)

    # -- models
    def _get_ai(self):
        if self._ai is None and self.s.use_ai:
            from .embedders import get_embedder

            self._ai = get_embedder(self.s.embedder, use_gpu=self.s.use_gpu)
            if self._ai:
                self.info("Image AI loaded: %s (%s)", self.s.embedder, "GPU" if self._ai.on_gpu else "CPU")
        return self._ai

    def _get_faces(self):
        if self._faces is None and self.s.use_faces:
            from .faces import FaceEngine, faces_available

            if not faces_available(self.s.face_pack):
                return None
            self._faces = FaceEngine(self.s.face_pack, use_gpu=self.s.use_gpu)
            self.info("Face models loaded (%s)", self.s.face_pack)
        return self._faces

    def stage_embed(self):
        if not self.s.use_ai:
            self.info("Image AI disabled; type sorting uses heuristics only")
            return
        ai = self._get_ai()
        if ai is None:
            self.info("Image AI models (%s) not installed; skipping (use 'Download models')", self.s.embedder)
            return
        self.db.set_meta("embedder", self.s.embedder)
        allrows = self.db.query("SELECT f.id, f.src_path, f.media, f.duration, v.hdr FROM files f LEFT JOIN videos v "
                                "ON v.file_id=f.id WHERE f.analyzed=1 AND f.embedded=0 AND f.id=f.rep_id ORDER BY f.id")
        vids = [r for r in allrows if r["media"] == "video" and self.videos_on]
        rows = [r for r in allrows if r["media"] != "video"]
        total, done = len(allrows) if self.videos_on else len(rows), 0
        B = self.s.ai_batch
        t0 = time.time()

        def load(r):
            try:
                return r, open_image(Path(r["src_path"]), max_side=448)
            except Exception:
                return r, None

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            for i in range(0, len(rows), B):
                self.checkpoint()
                loaded = [x for x in ex.map(load, rows[i:i + B]) if x[1] is not None]
                if loaded:
                    embs = ai.encode_images([im for _, im in loaded])
                    self.db.executemany("INSERT OR REPLACE INTO embeddings(file_id, vec) VALUES(?, ?)",
                                        [(r["id"], vec_to_blob(e)) for (r, _), e in zip(loaded, embs)])
                self.db.executemany("UPDATE files SET embedded=1, classified=0 WHERE id=?", [(r["id"],) for r in rows[i:i + B]])
                self.db.commit()
                done += len(rows[i:i + B])
                rate = done / max(time.time() - t0, 1e-6)
                self.progress("embed", done, total, per_sec=round(rate, 1))
        if vids:
            self._embed_videos(ai, vids, done, total)
        self.progress("embed", total, total)

    def _video_frames(self, r, max_side: int):
        try:
            return r, video.frames(Path(r["src_path"]), video.sample_times(r["duration"], self.s.video_frames),
                                   max_side, bool(r["hdr"]))
        except Exception as e:
            log.warning("video frames failed on %s: %s", r["src_path"], e)
            return r, []

    def _embed_videos(self, ai, vids, done: int, total: int):
        """v0.6: a video's search vector is the mean of its sampled frames' vectors (same space as photos)."""
        with ThreadPoolExecutor(max_workers=max(1, min(2, self.workers))) as ex:
            for i in range(0, len(vids), 4):
                self.checkpoint()
                self.gate.wait()                 # decoding video is real work: let the person at the PC go first
                for r, frs in ex.map(lambda r: self._video_frames(r, 448), vids[i:i + 4]):
                    if frs:
                        embs = np.asarray(ai.encode_images([im for _, im in frs]), dtype=np.float32)
                        v = embs.mean(axis=0)
                        v /= max(float(np.linalg.norm(v)), 1e-8)
                        self.db.execute("INSERT OR REPLACE INTO embeddings(file_id, vec) VALUES(?, ?)",
                                        (r["id"], vec_to_blob(v)))
                    self.db.execute("UPDATE files SET embedded=1, classified=0 WHERE id=?", (r["id"],))
                self.db.commit()
                done += len(vids[i:i + 4])
                self.progress("embed", done, total, videos=True)

    def stage_classify(self):
        rows = self.db.query("SELECT f.*, e.vec FROM files f LEFT JOIN embeddings e ON e.file_id=f.id "
                             "WHERE f.analyzed=1 AND f.classified=0 AND f.id=f.rep_id")
        total = len(rows)
        ai = self._get_ai() if any(r["vec"] for r in rows) else None
        type_labels = type_mat = tag_labels = tag_mat = None
        if ai:
            type_labels, type_mat = ai.type_matrix()
            tag_labels, tag_mat = ai.tag_matrix()
        for i, r in enumerate(rows):
            if i % 200 == 0:
                self.checkpoint()
                self.progress("classify", i, total)
            feats = cls.heuristic_features(r["filename"], r["width"], r["height"], r["format"],
                                           r["camera_make"], r["camera_model"], r["software"])
            probs = None
            if ai and r["vec"]:
                v = blob_to_vec(r["vec"])
                v /= max(float(np.linalg.norm(v)), 1e-8)
                p = ai.type_probs(v[None, :], type_mat)[0]
                probs = dict(zip(type_labels, p.tolist()))
                self.db.execute("DELETE FROM tags WHERE file_id=?", (r["id"],))
                self.db.executemany("INSERT OR REPLACE INTO tags(file_id, tag, score) VALUES(?,?,?)",
                                    [(r["id"], tag_labels[j], sc) for j, sc in ai.pick_tags(v, tag_mat)])
            t, src, scores = cls.classify(feats, probs)
            if r["media"] == "video":
                t, src, scores = "video", "video", {}
            self.db.execute("UPDATE files SET type=?, type_source=?, type_scores=?, classified=1 WHERE id=?",
                            (t, src, json.dumps(scores), r["id"]))
        # exact copies inherit their representative's type
        self.db.execute("UPDATE files SET type=(SELECT r.type FROM files r WHERE r.id=files.rep_id), "
                        "type_source='same-as-copy', classified=1 WHERE rep_id IS NOT NULL AND rep_id<>id")
        self.db.commit()
        self.progress("classify", total, total)

    def stage_faces(self):
        if not self.s.use_faces:
            self.info("Face recognition disabled")
            return
        eng = self._get_faces()
        if eng is None:
            self.info("Face models not downloaded; skipping (use 'Download models')")
            return
        self.db.set_meta("face_backend", self.s.face_pack)
        allrows = self.db.query("SELECT f.id, f.src_path, f.media, f.duration, v.hdr FROM files f LEFT JOIN videos v "
                                "ON v.file_id=f.id WHERE f.analyzed=1 AND f.faced=0 AND f.id=f.rep_id "
                                "AND (f.type='photo' OR f.media='video') ORDER BY f.id")
        rows = [r for r in allrows if r["media"] != "video"]
        vids = [r for r in allrows if r["media"] == "video" and self.videos_on]
        total, done, nfaces = len(rows) + len(vids), 0, 0

        def load(r):
            try:
                return r, open_image(Path(r["src_path"]), max_side=DETECT_MAX_SIDE)
            except Exception:
                return r, None

        with ThreadPoolExecutor(max_workers=min(4, self.workers)) as ex:
            for i in range(0, len(rows), 8):
                self.checkpoint()
                for r, im in ex.map(load, rows[i:i + 8]):
                    if im is not None:
                        try:
                            found = eng.faces(im, self.s.face_min_score, self.s.face_min_size)
                        except Exception as e:
                            log.warning("face detection failed on %s: %s", r["src_path"], e)
                            found = []
                        W, H = im.size
                        arr = np.asarray(im)
                        self.db.execute("DELETE FROM faces WHERE file_id=?", (r["id"],))
                        for f in found:
                            if "embedding" not in f:
                                continue
                            x1, y1, x2, y2 = f["bbox"]
                            self.db.execute(
                                "INSERT INTO faces(file_id, x1, y1, x2, y2, score, embedding, face_px, blur) "
                                "VALUES(?,?,?,?,?,?,?,?,?)",
                                (r["id"], max(0, x1 / W), max(0, y1 / H), min(1, x2 / W), min(1, y2 / H), f["score"],
                                 vec_to_blob(f["embedding"]), float(min(x2 - x1, y2 - y1)),
                                 blur_score(arr, (x1, y1, x2, y2))))
                            nfaces += 1
                    self.db.execute("UPDATE files SET faced=1 WHERE id=?", (r["id"],))
                    done += 1
                self.db.commit()
                self.progress("faces", done, total, faces=nfaces)
        if vids:
            nfaces += self._faces_in_videos(eng, vids, done, total)
        self.progress("faces", total, total, faces=nfaces)
        if total:
            self.info("Faces: %d faces found in %d photos", nfaces, total)
        if vids:
            self.info("Faces: %d of those photos are videos (a few frames of each were searched)", len(vids))
        self._measure_old_faces()

    def _faces_in_videos(self, eng, vids, done: int, total: int) -> int:
        """v0.6: the same face models and the same people as photos. A few frames per video are searched; the
        same person seen in several frames is kept once (their clearest face), so videos don't swamp People."""
        from . import thumbs

        found_total = 0
        fdir = thumbs.cache_dir(self.out) / "faces"
        fdir.mkdir(exist_ok=True)
        with ThreadPoolExecutor(max_workers=max(1, min(2, self.workers))) as ex:
            for i in range(0, len(vids), 2):
                self.checkpoint()
                self.gate.wait()
                for r, frs in ex.map(lambda r: self._video_frames(r, DETECT_MAX_SIDE), vids[i:i + 2]):
                    cands = []
                    for t, im in frs:
                        try:
                            got = eng.faces(im, self.s.face_min_score, self.s.face_min_size)
                        except Exception as e:
                            log.warning("face detection failed on %s: %s", r["src_path"], e)
                            continue
                        W, H = im.size
                        arr = np.asarray(im)
                        for f in got:
                            if "embedding" not in f:
                                continue
                            x1, y1, x2, y2 = f["bbox"]
                            px = float(min(x2 - x1, y2 - y1))
                            bl = blur_score(arr, (x1, y1, x2, y2))
                            cands.append({"t": t, "im": im, "box": (x1, y1, x2, y2), "W": W, "H": H, "px": px,
                                          "blur": bl, "score": f["score"], "emb": np.asarray(f["embedding"], np.float32),
                                          "q": f["score"] * px * min(bl if bl is not None and bl >= 0 else 50, 300)})
                    kept = pick_video_faces(cands)
                    self.db.execute("DELETE FROM faces WHERE file_id=?", (r["id"],))
                    for c in kept:
                        x1, y1, x2, y2 = c["box"]
                        W, H = c["W"], c["H"]
                        fid = self.db.execute(
                            "INSERT INTO faces(file_id, x1, y1, x2, y2, score, embedding, face_px, blur, frame_t) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (r["id"], max(0, x1 / W), max(0, y1 / H), min(1, x2 / W), min(1, y2 / H), c["score"],
                             vec_to_blob(c["emb"]), c["px"], c["blur"], c["t"])).lastrowid
                        try:                          # face picture for the People page (no second decode later)
                            pad = 0.25 * max(x2 - x1, y2 - y1)
                            crop = c["im"].crop((max(0, x1 - pad), max(0, y1 - pad), min(W, x2 + pad), min(H, y2 + pad)))
                            crop.thumbnail((160, 160))
                            crop.save(fdir / f"{fid}.jpg", "JPEG", quality=85)
                        except Exception:
                            pass
                    found_total += len(kept)
                    self.db.execute("UPDATE files SET faced=1 WHERE id=?", (r["id"],))
                self.db.commit()
                done += len(vids[i:i + 2])
                self.progress("faces", done, total, faces=found_total, videos=True)
        return found_total

    def _measure_old_faces(self):
        """Faces found before 0.3 have no size/sharpness yet: measure them once (re-reads those photos)."""
        backfill_face_px(self.db)
        rows = self.db.query("SELECT DISTINCT f.id, f.src_path FROM faces fa JOIN files f ON f.id=fa.file_id "
                             "WHERE fa.blur IS NULL AND f.media IS NULL ORDER BY f.id")
        if not rows:
            return
        self.info("Faces: measuring sharpness of faces found by an older version (%d photos, once)...", len(rows))
        total, done = len(rows), 0

        def load(r):
            try:
                return r, open_image(Path(r["src_path"]), max_side=DETECT_MAX_SIDE)
            except Exception:
                return r, None

        with ThreadPoolExecutor(max_workers=min(4, self.workers)) as ex:
            for i in range(0, total, 16):
                self.checkpoint()
                for r, im in ex.map(load, rows[i:i + 16]):
                    faces = self.db.query("SELECT id, x1, y1, x2, y2 FROM faces WHERE file_id=? AND blur IS NULL", (r["id"],))
                    if im is None:
                        self.db.executemany("UPDATE faces SET blur=-1 WHERE id=? AND blur IS NULL",
                                            [(fa["id"],) for fa in faces])   # unreadable now: judged on size/score only
                        continue
                    W, H = im.size
                    arr = np.asarray(im)
                    for fa in faces:
                        box = (fa["x1"] * W, fa["y1"] * H, fa["x2"] * W, fa["y2"] * H)
                        self.db.execute("UPDATE faces SET blur=?, face_px=? WHERE id=?",
                                        (blur_score(arr, box), float(min(box[2] - box[0], box[3] - box[1])), fa["id"]))
                done += len(rows[i:i + 16])
                self.db.commit()
                self.progress("faces", done, total, measuring=True)
        self.db.set_meta("clustered_face_count", None)     # re-group with the quality marks

    def stage_people(self):
        cnt = self.db.one("SELECT COUNT(*) FROM faces")[0]
        if cnt == 0 or self.db.get_meta("clustered_face_count") == cnt:
            return
        self.info("Grouping %d faces into people...", cnt)
        eps, min_samples, method = face_cluster_params(self.db, self.s)
        st = cluster_faces(self.db, eps, min_samples, method)
        self.db.set_meta("clustered_face_count", cnt)
        n = self.db.one("SELECT COUNT(*) FROM people")[0]
        self.progress("people", cnt, cnt, people=n)
        self.info("People: %d groups (name them in the web app); method %s, strictness %.2f, %d low-quality faces "
                  "skipped, %d faces to confirm", n, method, eps, st.get("low_quality", 0), st.get("borderline", 0))

    def stage_dedupe(self):
        """Group duplicates. Group numbers stay stable across runs (Duplicates\\group-N never shifts), and the
        keeper of a group already organized or reviewed in an earlier run is never swapped silently: a better
        newcomer is only *suggested* (shown as "needs review" in the web app's Clean up page)."""
        self.progress("dedupe", 0, 1)
        rows = [dict(r) for r in self.db.query(
            "SELECT f.id, f.src_path, f.sha256, f.rep_id, f.width, f.height, f.size, f.date_source, f.camera_make, "
            "f.camera_model, f.lat, f.phash, f.dhash, f.type, f.dup_group, f.is_keeper, f.dest_path, f.organized, "
            "f.filename, f.ext, f.taken_at, f.software, f.media, f.duration, fr.state AS review "
            "FROM files f LEFT JOIN file_review fr ON fr.file_id=f.id WHERE f.analyzed=1")]
        byid = {r["id"]: r for r in rows}
        uf = UnionFind()
        for r in rows:
            if r["rep_id"] and r["rep_id"] != r["id"]:
                uf.union(r["id"], r["rep_id"])
        reps = [r for r in rows if r["rep_id"] == r["id"] and r["phash"] is not None]
        pairs = near_pairs([r["id"] for r in reps], [r["phash"] for r in reps], [r["dhash"] for r in reps],
                           self.s.phash_max_distance, self.s.dhash_max_distance)
        embs = {}
        if pairs:
            ids = {a for a, _, _ in pairs} | {b for _, b, _ in pairs}
            for e in self.db.query(f"SELECT file_id, vec FROM embeddings WHERE file_id IN ({','.join(map(str, ids))})"):
                embs[e["file_id"]] = blob_to_vec(e["vec"])
        confirmed = 0
        for a, b, dist in pairs:
            self.checkpoint()
            screenshotish = "screenshot" in (byid[a]["type"], byid[b]["type"])
            if a in embs and b in embs:
                sim = float(embs[a] @ embs[b]) / (np.linalg.norm(embs[a]) * np.linalg.norm(embs[b]) + 1e-8)
                ok = sim >= (0.97 if screenshotish else self.s.ai_dupe_min_similarity) and (dist <= 4 or not screenshotish)
            else:
                ok = dist <= (2 if screenshotish else 4)
            if ok:
                uf.union(a, b); confirmed += 1
        confirmed += self._video_pairs(rows, uf)
        old_keeper = {r["id"]: r["keeper_id"] for r in self.db.query("SELECT id, keeper_id FROM dup_groups")}
        groups = assign_stable_group_ids(uf.groups(), {r["id"]: r["dup_group"] for r in rows},
                                         int(self.db.get_meta("max_dup_group", 0) or 0), max(old_keeper, default=0))
        self.db.execute("UPDATE files SET dup_group=NULL, is_keeper=NULL")
        self.db.execute("DELETE FROM dup_groups")
        suggested = 0
        from .prefs import load_model

        model = load_model(self.db)
        if model.active:
            self.info("Duplicates: choosing keepers with what you taught on the Clean up page (%d decisions)",
                      model.groups)
        for gid, members in sorted(groups.items()):
            mem = [byid[m] for m in members]
            keeper, suggestion, note = choose_keeper(mem, old_keeper.get(gid), model, self.out)
            suggested += suggestion is not None
            kind = "exact" if len({m["sha256"] for m in mem}) == 1 else "near"
            self.db.execute("INSERT INTO dup_groups(id, kind, keeper_id, size, suggested_keeper_id, note) "
                            "VALUES(?,?,?,?,?,?)", (gid, kind, keeper, len(mem), suggestion, note))
            self.db.executemany("UPDATE files SET dup_group=?, is_keeper=? WHERE id=?",
                                [(gid, 1 if (m["id"] == keeper or m["review"] == "keep") else 0, m["id"]) for m in mem])
        self.db.set_meta("max_dup_group", max([*groups, int(self.db.get_meta("max_dup_group", 0) or 0)]))
        self.db.commit()
        self.progress("dedupe", 1, 1, groups=len(groups))
        self.info("Duplicates: %d groups (%d look-alike pairs confirmed)", len(groups), confirmed)
        if suggested:
            self.info("Duplicates: %d earlier groups got a better copy; review them on the Clean up page", suggested)

    def _video_pairs(self, rows: list[dict], uf: UnionFind) -> int:
        """v0.6: near-duplicate videos (re-encoded, resized or re-sent copies): about the same length and the same
        pictures at the same moments; when both have AI vectors those must agree too."""
        vids = [r for r in rows if r.get("media") == "video" and r["rep_id"] == r["id"] and r.get("duration")]
        if len(vids) < 2:
            return 0
        sigs = {r["file_id"]: json.loads(r["sig"] or "[]") for r in self.db.query("SELECT file_id, sig FROM videos")}
        embs = {e["file_id"]: blob_to_vec(e["vec"]) for e in self.db.query(
            f"SELECT file_id, vec FROM embeddings WHERE file_id IN ({','.join(str(r['id']) for r in vids)})")}
        vids.sort(key=lambda r: r["duration"])
        n = 0
        for i, a in enumerate(vids):
            for b in vids[i + 1:]:
                if b["duration"] - a["duration"] > max(1.0, 0.02 * b["duration"]):
                    break
                if not video.same_video({"duration": a["duration"], "sig": sigs.get(a["id"])},
                                        {"duration": b["duration"], "sig": sigs.get(b["id"])}):
                    continue
                if a["id"] in embs and b["id"] in embs:
                    ea, eb = embs[a["id"]], embs[b["id"]]
                    if float(ea @ eb) / (np.linalg.norm(ea) * np.linalg.norm(eb) + 1e-8) < 0.85:
                        continue
                uf.union(a["id"], b["id"])
                n += 1
        return n

    def stage_organize(self):
        rows = self.db.query(
            "SELECT f.*, g.kind AS gkind, g.keeper_id FROM files f LEFT JOIN dup_groups g ON g.id=f.dup_group "
            "WHERE f.organized=0 AND f.analyzed<>0 ORDER BY f.id")
        total, done, copied = len(rows), 0, 0
        used = {r[0].lower() for r in self.db.query("SELECT dest_path FROM files WHERE dest_path IS NOT NULL")}
        keeper_sha = {r["id"]: r["sha256"] for r in self.db.query(
            "SELECT f.id, f.sha256 FROM files f JOIN dup_groups g ON g.keeper_id=f.id")}
        exact_rows = []
        for r in rows:
            if done % 50 == 0:
                self.checkpoint()
                self.progress("organize", done, total, copied=copied)
            done += 1
            rel = self.dest_for(r, keeper_sha)
            # readable now, but an older run copied it to Other\ as unreadable: move that copy to its real place
            old_other = (self.out / r["dest_path"]) if (r["dest_path"] and r["analyzed"] == 1 and
                                                         Path(r["dest_path"]).parts[0].lower() == "other") else None
            if old_other is not None and not old_other.exists():
                old_other = None
            if rel is None:  # exact copy, not copied
                exact_rows.append(r)
                if old_other is not None:   # keep the copy we already made rather than deleting anything
                    self.db.execute("UPDATE files SET organized=1 WHERE id=?", (r["id"],))
                else:
                    self.db.execute("UPDATE files SET organized=1, dest_path=NULL WHERE id=?", (r["id"],))
                continue
            dest = self._unique_dest(rel, r, used)
            try:
                if old_other is not None and not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(old_other), str(dest))
                    used.discard(r["dest_path"].lower())
                elif not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_name(dest.name + ".partial")
                    shutil.copy2(r["src_path"], tmp)
                    os.replace(tmp, dest)
                    copied += 1
                relp = str(dest.relative_to(self.out))
                used.add(relp.lower())
                self.db.execute("UPDATE files SET organized=1, dest_path=? WHERE id=?", (relp, r["id"]))
            except OSError as e:
                self.db.execute("UPDATE files SET error=? WHERE id=?", (f"copy failed: {e}"[:500], r["id"]))
            if done % 50 == 0:
                self.db.commit()
        self.db.commit()
        self._write_reports(exact_rows)
        self.progress("organize", total, total, copied=copied)
        self.info("Organize: %d files copied into %s (originals untouched)", copied, self.out)

    def stage_thumbs(self):
        """Pre-generate grid thumbnails into _index/thumbs (only that cache is written; photos are only read)."""
        from . import thumbs

        rows = self.db.query("SELECT f.id, f.src_path, f.dest_path, f.mtime, f.size, f.media, f.duration, v.hdr "
                             "FROM files f LEFT JOIN videos v ON v.file_id=f.id WHERE f.analyzed=1 AND "
                             "(f.is_keeper IS NULL OR f.is_keeper=1) AND (f.rep_id IS NULL OR f.rep_id=f.id) ORDER BY f.id")
        if not self.videos_on:
            rows = [r for r in rows if r["media"] != "video"]
        todo = [r for r in rows if not thumbs.cache_path(self.out, r["id"]).exists()]
        total, done, failed = len(todo), 0, 0
        if not total:
            return

        def work(r):
            try:
                thumbs.ensure(self.out, r)
                return True
            except Exception:
                return False

        t0 = time.time()
        step = 64 if self.s.run_mode == "full" else 16
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            for i in range(0, total, step):
                self.checkpoint()
                self.gate.wait()            # background mode: let the person at the PC go first
                res = list(ex.map(work, todo[i:i + step]))
                done += len(res); failed += res.count(False)
                self.progress("thumbs", done, total)
        self.info("Thumbnails: %d made in %.1fs (%d could not be read)", total - failed, time.time() - t0, failed)

    def stage_videos(self):
        """v0.6: scrub/hover sprites for every video, and a small H.264 copy for the ones a browser can't play
        (e.g. iPhone HEVC). Only _index/video is written. Waits while you use the PC in background mode."""
        if not self.videos_on:
            return
        rows = self.db.query(
            "SELECT f.id, f.src_path, f.playable, v.sprite_n, v.proxy FROM files f LEFT JOIN videos v ON v.file_id=f.id "
            "WHERE f.media='video' AND f.analyzed=1 AND (f.is_keeper IS NULL OR f.is_keeper=1) "
            "AND (f.rep_id IS NULL OR f.rep_id=f.id) AND NOT EXISTS (SELECT 1 FROM file_review fr "
            "WHERE fr.file_id=f.id AND fr.state='remove') ORDER BY f.taken_at DESC, f.id DESC")
        sprites = [r for r in rows if not r["sprite_n"] or not video.sprite_path(self.out, r["id"]).exists()]
        proxies = [r for r in rows if self.s.video_proxies and not r["playable"] and r["proxy"] != "failed"
                   and video.proxy_state(self.db, self.out, r["id"]) == "missing"]
        total = len(sprites) + len(proxies)
        if not total:
            return
        if proxies and not video.h264_encoder():
            self.info("Videos: no H.264 encoder in this ffmpeg; videos your browser can't play stay as they are")
            total -= len(proxies)
            proxies = []
        done = made = failed = 0
        t0 = time.time()
        for r in sprites:
            self.checkpoint()
            self.gate.wait()
            made += video.ensure_sprite(self.db, self.out, r["id"]) is not None
            done += 1
            self.progress("videos", done, total)
        for r in proxies:              # slowest part last: everything else is usable meanwhile
            self.checkpoint()
            self.gate.wait()
            failed += video.ensure_proxy(self.db, self.out, r["id"]) == "failed"
            done += 1
            self.progress("videos", done, total, playable_copies=done - len(sprites))
        self.info("Video previews: %d scrub strips, %d playable copies (%d failed) in %.0fs (%s)", made,
                  len(proxies) - failed, failed, time.time() - t0, video.h264_encoder() or "no encoder")

    def dest_for(self, r, keeper_sha: dict) -> Path | None:
        name = r["filename"]
        if r["analyzed"] == -1:
            return Path("Other") / name
        if r["dup_group"] and not r["is_keeper"]:
            same_as_keeper = r["sha256"] == keeper_sha.get(r["keeper_id"])
            exact_copy_of_other = r["rep_id"] != r["id"]
            if (same_as_keeper or exact_copy_of_other) and not self.s.copy_exact_duplicates:
                return None
            return Path("Duplicates") / f"group-{r['dup_group']:05d}" / name
        return main_dest(r)

    def _unique_dest(self, rel: Path, r, used: set) -> Path:
        return unique_dest(self.out, rel, r["size"], used)

    def _write_reports(self, exact_rows=None):
        write_reports(self.db, self.out)


SAME_PERSON_IN_VIDEO = 0.45      # cosine similarity: two faces in one video this alike are the same person


def pick_video_faces(cands: list[dict], max_faces: int = 12) -> list[dict]:
    """One face per person per video: clearest first, drop later ones that match a kept face."""
    kept: list[dict] = []
    for c in sorted(cands, key=lambda c: -c["q"]):
        e = c["emb"] / max(float(np.linalg.norm(c["emb"])), 1e-8)
        if any(float(e @ k["_e"]) >= SAME_PERSON_IN_VIDEO for k in kept):
            continue
        c["_e"] = e
        kept.append(c)
        if len(kept) >= max_faces:
            break
    return kept


def main_dest(r) -> Path:
    """Where a photo lives in the main tree (keepers and non-duplicates). Same rules every run."""
    name = r["filename"]
    t = r["type"] or "photo"
    dt = datetime.fromisoformat(r["taken_at"]) if r["taken_at"] else None
    if t == "screenshot":
        return Path("Screenshots") / (str(dt.year) if dt else "Unknown Date") / name
    if t in TYPE_FOLDERS:
        return Path(TYPE_FOLDERS[t]) / name
    if not dt:
        return Path("Photos") / "Unknown Date" / name
    month = f"{dt.year:04d}-{dt.month:02d}"
    if r["place"]:
        month = f"{month} {safe_name(r['place'], 60)}"
    return Path("Photos") / f"{dt.year:04d}" / month / name


def unique_dest(out: Path, rel: Path, size, used: set) -> Path:
    dest = Path(out) / rel
    stem, suf = dest.stem, dest.suffix
    n = 1
    while True:
        relp = str(dest.relative_to(out)).lower()
        if relp not in used and not dest.exists():
            return dest
        if size is not None and dest.exists() and relp not in used and dest.stat().st_size == size:
            return dest  # left over from an interrupted earlier run
        dest = dest.with_name(f"{stem} ({n}){suf}")
        n += 1


def in_main_tree(dest_path: str | None) -> bool:
    if not dest_path:
        return False
    top = Path(dest_path).parts[0].lower()
    return top not in ("duplicates", "other", INDEX_DIRNAME.lower())


def assign_stable_group_ids(groups: list[list[int]], prev_group: dict, max_ever: int, max_old: int) -> dict:
    """Map each new duplicate group to an id. A group that contains members of an earlier group keeps that
    group's id (the one with most members; ties -> lowest id). Brand-new groups get ids never used before."""
    taken: set[int] = set()
    out: dict[int, list[int]] = {}
    pending = []
    # biggest overlap first so a split group's id stays with its larger part
    scored = []
    for members in groups:
        votes: dict[int, int] = {}
        for m in members:
            g = prev_group.get(m)
            if g:
                votes[g] = votes.get(g, 0) + 1
        scored.append((members, votes))
    scored.sort(key=lambda x: -max(x[1].values(), default=0))
    for members, votes in scored:
        for g in sorted(votes, key=lambda g: (-votes[g], g)):
            if g not in taken:
                taken.add(g); out[g] = members
                break
        else:
            pending.append(members)
    nxt = max(max_ever, max_old, max(taken, default=0))
    for members in sorted(pending, key=lambda m: m[0]):
        nxt += 1
        out[nxt] = members
    return out


def choose_keeper(mem: list[dict], old_keeper: int | None, model=None, out=None
                  ) -> tuple[int, int | None, str | None]:
    """(keeper_id, suggested_keeper_id or None, note). Never silently swaps a keeper the user already has in
    their main tree or approved; a better newcomer becomes a suggestion instead. `model` = learned keep
    preferences (v0.4), used to rank copies once enough Clean up decisions exist."""
    candidates = [m for m in mem if m.get("review") != "remove"] or mem
    best = pick_keeper(candidates, model, out)
    anchored = [m for m in candidates if m.get("review") == "keep" or
                (m.get("organized") == 1 and in_main_tree(m.get("dest_path")))]
    if not anchored:
        return best, None, None
    ids = {m["id"] for m in anchored}
    keeper = old_keeper if old_keeper in ids else pick_keeper(anchored, model, out)
    best_row = next(m for m in candidates if m["id"] == best)
    if best != keeper and best_row.get("review") is None:
        return keeper, best, "A better copy was added in a later batch; swap the keeper?"
    return keeper, None, None


def write_reports(db: DB, out: Path):
    """Rewrite _index/reports/exact-duplicates.csv from the index (reflects Clean up decisions)."""
    rep = index_dir(out) / "reports"
    rep.mkdir(exist_ok=True)
    rows = db.query(
        "SELECT f.src_path, f.dup_group, k.dest_path AS kept, fr.state FROM files f "
        "JOIN dup_groups g ON g.id=f.dup_group LEFT JOIN files k ON k.id=g.keeper_id "
        "LEFT JOIN file_review fr ON fr.file_id=f.id "
        "WHERE f.organized=1 AND f.dest_path IS NULL AND f.analyzed=1 "
        "AND (g.keeper_id IS NULL OR f.id<>g.keeper_id) "
        "AND f.sha256 IN (SELECT sha256 FROM files x WHERE x.dup_group=f.dup_group AND x.id<>f.id) "
        "ORDER BY f.dup_group, f.src_path")
    path = rep / "exact-duplicates.csv"
    if rows or path.exists():
        tmp = path.with_suffix(".csv.tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["duplicate_original_path", "kept_copy_in_tree", "group", "review"])
            for r in rows:
                w.writerow([r["src_path"], r["kept"] or "", r["dup_group"],
                            {"remove": "confirmed duplicate", "keep": "kept"}.get(r["state"], "not reviewed")])
        os.replace(tmp, path)
    for g in db.query("SELECT g.id, f.dest_path, f.src_path FROM dup_groups g JOIN files f ON f.id=g.keeper_id "
                      "WHERE g.kind='near'"):
        d = Path(out) / "Duplicates" / f"group-{g['id']:05d}"
        if d.is_dir():
            (d / "_kept.txt").write_text(
                f"The best copy was kept in the main tree:\n{g['dest_path']}\n\nOriginal: {g['src_path']}\n",
                encoding="utf-8")


def unreadable_category(reason: str) -> str:
    r = reason.lower()
    for key, label in (("video", "videos"), ("heic", "HEIC"), ("avif", "AVIF"), ("empty", "empty"),
                       ("zeros", "damaged"), ("cut off", "cut off"), ("web page", "not images"),
                       ("not a recognised", "unknown format"), ("permission", "no permission")):
        if key in r:
            return label
    return "other"


def write_unreadable_report(db: DB, out: Path):
    """_index/reports/unreadable.csv: every file that could not be read, with a plain-English reason."""
    from collections import Counter

    rows = db.query("SELECT src_path, ext, size, error, dest_path FROM files WHERE analyzed=-1 ORDER BY src_path")
    rep = index_dir(out) / "reports"
    rep.mkdir(exist_ok=True)
    path = rep / "unreadable.csv"
    kinds: Counter = Counter()
    if rows or path.exists():
        tmp = path.with_suffix(".csv.tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["original_path", "reason", "extension", "size_bytes", "copy_in_output", "technical_error"])
            for r in rows:
                reason = unreadable_reason(Path(r["src_path"]), r["error"])
                kinds[unreadable_category(reason)] += 1
                w.writerow([r["src_path"], reason, r["ext"] or "", r["size"] or "", r["dest_path"] or "", r["error"] or ""])
        os.replace(tmp, path)
    return len(rows), kinds


def build_people_folders(output: Path, db: DB) -> int:
    """People/<Name>/ with NTFS hard links (no extra disk space). Returns links made."""
    made = 0
    for p in db.query("SELECT id, name FROM people WHERE name IS NOT NULL AND name<>''"):
        d = Path(output) / "People" / safe_name(p["name"])
        d.mkdir(parents=True, exist_ok=True)
        for r in db.query("SELECT DISTINCT f.dest_path FROM faces fa JOIN files f ON f.id=fa.file_id "
                          "WHERE fa.person_id=? AND f.dest_path IS NOT NULL", (p["id"],)):
            src = Path(output) / r[0]
            link = d / src.name
            if link.exists() or not src.exists():
                continue
            try:
                os.link(src, link)
                made += 1
            except OSError:
                pass
    return made
