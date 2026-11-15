"""Verify before deleting originals (v0.4). Strictly read-only over the original folders.

For every file under the original (source) folders:
  1 safe_copy      a byte-identical copy (same SHA-256) exists somewhere in the output tree (Photos, Duplicates,
                   Albums, People, ...; hard links are counted once)
  2 safe_deduped   its sorted copy was removed on purpose in Clean up (file_review = remove) and the photo kept
                   for that duplicate group still exists in the library
  3 missing        no copy: unreadable, not a photo type (videos, documents...), not indexed yet, copy failed,
                   removed with "Delete all", or a read error
  4 near_only      only a look-alike (same duplicate group, not byte-identical) and no Clean up decision
Buckets 1-2 are safe; 3-4 need attention. "Rescue" copies buckets 3-4 into <output>/Rescued/<source>/<relative
path> (copy only). Nothing here ever deletes, moves or writes to an original, and the app never deletes the
original folders themselves.

Hashes are cached in <output>/_index/verify.sqlite by (path, size, mtime) so a re-run only reads new or changed
files. Runs at low priority in background mode and can be stopped and resumed.
"""
from __future__ import annotations

import csv
import hashlib
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from . import background
from .config import INDEX_DIRNAME, app_data_dir, db_path, index_dir
from .db import DB

BUCKETS = ("safe_copy", "safe_deduped", "missing", "near_only")
BUCKET_LABELS = {
    "safe_copy": "Safe: an identical copy is in your library",
    "safe_deduped": "Safe: removed on purpose as a duplicate (the kept copy is in your library)",
    "missing": "Needs attention: no copy in your library",
    "near_only": "Needs attention: only a look-alike copy, not identical",
}
SAFE = {"safe_copy", "safe_deduped"}
# v0.6: originals whose sorted copy was deleted on purpose with Delete on a selection
BUCKETS = BUCKETS[:2] + ("safe_deleted",) + BUCKETS[2:]
BUCKET_LABELS["safe_deleted"] = "Safe: you deleted it on purpose with Delete (its sorted copy went to the Recycle Bin or trash)"
SAFE = SAFE | {"safe_deleted"}
CHUNK = 1 << 20
SKIP_DIRS = {"$recycle.bin", "system volume information", INDEX_DIRNAME.lower()}
RESCUE_DIR = "Rescued"
BACKUP_ADVICE = ("Even when every file is safe, keep one extra backup of the original folders (an external drive "
                 "or a cloud copy) for at least a month before deleting them. Photo Sorter never deletes your "
                 "original folders; deleting them is your decision.")


def _vdb(out: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(index_dir(out) / "verify.sqlite"), timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS hashes (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, sha256 TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS results (path TEXT PRIMARY KEY, root TEXT, rel TEXT, size INTEGER, sha256 TEXT,
            bucket TEXT, reason TEXT, match_path TEXT);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    return c


def sha256_file(p: Path, gate: background.IdleGate | None = None, stop: threading.Event | None = None) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:                      # read-only
        n = 0
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            n += 1
            if stop is not None and stop.is_set():
                raise InterruptedError
            if gate is not None and n % 64 == 0:
                gate.wait()
    return h.hexdigest()


def _cached_hash(vc, p: Path, st, gate, stop) -> tuple[str | None, str | None]:
    r = vc.execute("SELECT size, mtime, sha256, error FROM hashes WHERE path=?", (str(p),)).fetchone()
    if r and r["size"] == st.st_size and abs((r["mtime"] or 0) - st.st_mtime) < 1:
        return r["sha256"], r["error"]
    try:
        sha, err = sha256_file(p, gate, stop), None
    except InterruptedError:
        raise
    except OSError as e:
        sha, err = None, f"{type(e).__name__}: {e}"
    vc.execute("INSERT OR REPLACE INTO hashes(path, size, mtime, sha256, error) VALUES(?,?,?,?,?)",
               (str(p), st.st_size, st.st_mtime, sha, err))
    return sha, err


def _walk(root: Path, skip: Path | None = None):
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        if skip is not None:
            try:
                if dp.resolve() == skip or skip in dp.resolve().parents:
                    dirnames[:] = []
                    continue
            except OSError:
                pass
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        for fn in filenames:
            yield dp / fn


def default_sources(out: Path, db: DB | None = None) -> list[str]:
    """The folders this index was built from: the DB's source roots plus the window's saved folders."""
    srcs = []
    if db is not None:
        srcs += [r[0] for r in db.query("SELECT DISTINCT src_root FROM files WHERE src_root IS NOT NULL")]
    try:
        import json

        st = json.loads((app_data_dir() / "gui.json").read_text(encoding="utf-8"))
        if Path(st.get("output") or "").resolve() == Path(out).resolve():
            srcs += st.get("sources") or []
    except Exception:
        pass
    seen, res = set(), []
    for s in srcs:
        k = os.path.normcase(os.path.abspath(s))
        if k not in seen:
            seen.add(k)
            res.append(s)
    return res


class Verifier:
    def __init__(self, out, sources: list[str] | None = None, run_mode: str | None = None, idle_fn=None):
        self.out = Path(out)
        self.db = DB(db_path(self.out))
        self.sources = sources or default_sources(self.out, self.db)
        mode = run_mode or background.saved_mode()
        self.mode = mode
        kw = {"idle_fn": idle_fn} if idle_fn else {}
        self.stop = threading.Event()
        self.gate = background.IdleGate(mode, stop=self.stop, **kw)
        self.progress = {"phase": "idle", "done": 0, "total": 0, "bytes": 0, "started": None, "error": None}

    # ---- library side
    def _library_hashes(self, vc) -> dict:
        """sha256 -> one library path. Hard links (Albums, People) are read once."""
        lib, seen = {}, set()
        out_res = self.out.resolve()
        files = [p for p in _walk(self.out) if INDEX_DIRNAME not in p.relative_to(self.out).parts[:1]]
        self.progress.update(phase="library", done=0, total=len(files))
        for p in files:
            if self.stop.is_set():
                raise InterruptedError
            try:
                st = p.stat()
            except OSError:
                continue
            key = (st.st_dev, st.st_ino) if st.st_ino else None
            if key and key in seen:
                self.progress["done"] += 1
                continue
            if key:
                seen.add(key)
            sha, _ = _cached_hash(vc, p, st, self.gate, self.stop)
            if sha and sha not in lib:
                lib[sha] = str(p.relative_to(out_res) if p.is_absolute() and out_res in p.resolve().parents
                               else p.relative_to(self.out))
            self.progress["done"] += 1
            self.progress["bytes"] += st.st_size
            if self.progress["done"] % 200 == 0:
                vc.commit()
        vc.commit()
        return lib

    # ---- classification
    def _classify(self, path: str, sha: str | None, err: str | None, lib: dict, idx: dict, by_sha: dict
                  ) -> tuple[str, str, str | None]:
        if sha and sha in lib:
            return "safe_copy", "identical copy in the library", lib[sha]
        rows = [idx[path]] if path in idx else []
        if sha:
            rows += [r for r in by_sha.get(sha, []) if r["src_path"] != path]
        if err:
            return "missing", f"could not be read ({err})", None
        if not rows:
            ext = Path(path).suffix.lower()
            from .config import IMAGE_EXTS, VIDEO_EXTS

            if ext not in IMAGE_EXTS | VIDEO_EXTS:
                return "missing", f"not a photo type Photo Sorter copies ({ext or 'no extension'})", None
            return "missing", "not in the index yet (run Start to sort it)", None
        r = rows[0]
        for row in rows:
            if row["review"] == "remove" and row["dup_group"]:
                kept = self._kept_copy(row["dup_group"], lib)
                if kept:
                    return "safe_deduped", f"removed on purpose in Clean up (group {row['dup_group']}); kept copy exists", kept
        if any(row["review"] == "remove" and not row["dup_group"] for row in rows):
            # v0.6: deleted on purpose with Delete on a selection (only the sorted copy went; this original is untouched)
            return "safe_deleted", "deleted on purpose with Delete (the sorted copy is in the Recycle Bin or trash)", None
        if any(row["review"] == "remove" for row in rows):
            return "missing", "removed in Clean up and no kept copy of its group remains (Delete all?)", None
        if r["dup_group"]:
            kept = self._kept_copy(r["dup_group"], lib)
            if kept:
                return "near_only", f"only a look-alike in duplicate group {r['dup_group']}, not identical", kept
        if r["analyzed"] == -1:
            return "missing", "unreadable when sorting: " + (r["error"] or "unknown error")[:200], None
        if r["error"]:
            return "missing", (r["error"] or "")[:200], None
        if r["dest_path"]:
            return "missing", "its library copy is gone or changed", None
        return "missing", "never copied into the library", None

    def _kept_copy(self, gid: int, lib: dict) -> str | None:
        for m in self.db.query("SELECT f.sha256, f.dest_path FROM files f LEFT JOIN file_review fr ON fr.file_id=f.id "
                               "WHERE f.dup_group=? AND COALESCE(fr.state, '') <> 'remove'", (gid,)):
            if m["sha256"] and m["sha256"] in lib:
                return lib[m["sha256"]]
        return None

    def run(self) -> dict:
        background.lower_thread_priority(self.mode)
        vc = _vdb(self.out)
        self.progress.update(phase="starting", started=time.time(), error=None, bytes=0)
        try:
            lib = self._library_hashes(vc)
            idx, by_sha = {}, {}
            for r in self.db.query("SELECT f.id, f.src_path, f.sha256, f.dup_group, f.dest_path, f.analyzed, f.error, "
                                   "fr.state AS review FROM files f LEFT JOIN file_review fr ON fr.file_id=f.id"):
                d = dict(r)
                idx[d["src_path"]] = d
                if d["sha256"]:
                    by_sha.setdefault(d["sha256"], []).append(d)
            out_res = self.out.resolve()
            originals = []
            for root in self.sources:
                rp = Path(root)
                if rp.is_dir():
                    originals += [(rp, p) for p in _walk(rp, skip=out_res)]
            self.progress.update(phase="originals", done=0, total=len(originals))
            vc.execute("DELETE FROM results")
            for rp, p in originals:
                if self.stop.is_set():
                    raise InterruptedError
                try:
                    st = p.stat()
                    sha, err = _cached_hash(vc, p, st, self.gate, self.stop)
                    size = st.st_size
                except OSError as e:
                    sha, err, size = None, f"{type(e).__name__}: {e}", None
                bucket, reason, match = self._classify(str(p), sha, err, lib, idx, by_sha)
                vc.execute("INSERT OR REPLACE INTO results(path, root, rel, size, sha256, bucket, reason, match_path) "
                           "VALUES(?,?,?,?,?,?,?,?)", (str(p), str(rp), str(p.relative_to(rp)), size, sha, bucket,
                                                       reason, match))
                self.progress["done"] += 1
                self.progress["bytes"] += size or 0
                if self.progress["done"] % 200 == 0:
                    vc.commit()
            vc.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('finished_at', ?)",
                       (datetime.now().isoformat(timespec="seconds"),))
            vc.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('sources', ?)", ("\n".join(self.sources),))
            vc.commit()
            self.progress["phase"] = "done"
        except InterruptedError:
            vc.commit()
            self.progress["phase"] = "stopped"
        except Exception as e:
            vc.commit()
            self.progress.update(phase="error", error=str(e))
            raise
        finally:
            vc.close()
        res = summary(self.out)
        write_csv(self.out)
        return {"summary": res}


def summary(out) -> dict:
    vc = _vdb(Path(out))
    try:
        buckets = {b: {"files": 0, "bytes": 0, "label": BUCKET_LABELS[b], "safe": b in SAFE} for b in BUCKETS}
        for r in vc.execute("SELECT bucket, COUNT(*) n, COALESCE(SUM(size), 0) b FROM results GROUP BY bucket"):
            if r["bucket"] in buckets:
                buckets[r["bucket"]].update(files=r["n"], bytes=r["b"])
        meta = {r["key"]: r["value"] for r in vc.execute("SELECT key, value FROM meta")}
    finally:
        vc.close()
    total = sum(b["files"] for b in buckets.values())
    attention = buckets["missing"]["files"] + buckets["near_only"]["files"]
    if not meta.get("finished_at"):
        banner = "Not checked yet."
    elif attention == 0:
        banner = f"100% safe: all {total:,} original files have a copy in your library."
    else:
        banner = f"{attention:,} file{'s' if attention != 1 else ''} need{'s' if attention == 1 else ''} attention."
    return {"buckets": buckets, "total": total, "attention": attention, "all_safe": bool(meta.get("finished_at"))
            and attention == 0, "banner": banner, "finished_at": meta.get("finished_at"),
            "sources": (meta.get("sources") or "").split("\n") if meta.get("sources") else [],
            "advice": BACKUP_ADVICE}


def results(out, bucket: str = "", offset: int = 0, limit: int = 200) -> list[dict]:
    vc = _vdb(Path(out))
    try:
        if bucket:
            rows = vc.execute("SELECT * FROM results WHERE bucket=? ORDER BY path LIMIT ? OFFSET ?",
                              (bucket, limit, offset)).fetchall()
        else:
            rows = vc.execute("SELECT * FROM results ORDER BY bucket, path LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [dict(r) for r in rows]
    finally:
        vc.close()


def csv_path(out) -> Path:
    d = index_dir(Path(out)) / "reports"
    d.mkdir(exist_ok=True)
    return d / "verify-originals.csv"


def write_csv(out) -> Path:
    p = csv_path(out)
    vc = _vdb(Path(out))
    try:
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["original", "size_bytes", "result", "safe", "reason", "library_copy", "sha256"])
            for r in vc.execute("SELECT * FROM results ORDER BY bucket, path"):
                w.writerow([r["path"], r["size"], BUCKET_LABELS.get(r["bucket"], r["bucket"]),
                            "yes" if r["bucket"] in SAFE else "NO", r["reason"], r["match_path"] or "", r["sha256"] or ""])
        os.replace(tmp, p)
    finally:
        vc.close()
    return p


def rescue(out, stop: threading.Event | None = None) -> dict:
    """Copy every not-safe original (buckets 3-4) into <output>/Rescued/<source name>/<relative path>.
    Copy only: originals are opened read-only and never moved or deleted. Existing rescued copies with the same
    content are skipped; a different file already at that name gets a " (2)" name."""
    out = Path(out)
    dest_root = out / RESCUE_DIR
    vc = _vdb(out)
    copied = skipped = 0
    nbytes = 0
    errors = []
    try:
        rows = vc.execute("SELECT * FROM results WHERE bucket IN ('missing', 'near_only') ORDER BY path").fetchall()
    finally:
        vc.close()
    for r in rows:
        if stop is not None and stop.is_set():
            break
        src = Path(r["path"])
        root_name = Path(r["root"]).name or "source"
        dst = dest_root / root_name / r["rel"]
        try:
            if os.path.normcase(os.path.abspath(dst)) == os.path.normcase(os.path.abspath(src)):
                raise OSError("refusing to copy a file onto itself")
            if dst.exists():
                if dst.stat().st_size == src.stat().st_size and r["sha256"] and sha256_file(dst) == r["sha256"]:
                    skipped += 1
                    continue
                i = 2
                while True:
                    cand = dst.with_name(f"{dst.stem} ({i}){dst.suffix}")
                    if not cand.exists():
                        dst = cand
                        break
                    i += 1
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".partial")
            shutil.copy2(src, tmp)                 # reads the original; never modifies it
            os.replace(tmp, dst)
            copied += 1
            nbytes += r["size"] or 0
        except OSError as e:
            errors.append(f"{src}: {e}")
    return {"copied": copied, "skipped": skipped, "bytes": nbytes, "errors": errors[:50], "folder": str(dest_root)}


def run_verify(out, sources: list[str] | None = None) -> dict:
    return Verifier(out, sources).run()
