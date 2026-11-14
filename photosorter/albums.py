"""Albums: stored in SQLite (albums, album_items) and mirrored as <output>/Albums/<Album name>/ folders.

Album folders hold NTFS hard links to the library copies (no extra disk space; the photo stays in its normal
folder). If a hard link is not possible (e.g. a FAT/exFAT drive), the photo is copied instead and the result says
so. Deleting an album or removing a photo from it only removes the album's link; library photos and originals
are never touched. Clean up keeps the folders in sync (a removed copy loses its album links so space is freed).
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from .db import DB
from .geo import safe_name

log = logging.getLogger("photosorter")

ALBUMS_DIR = "Albums"


class AlbumError(Exception):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _root(out: Path) -> Path:
    return Path(out) / ALBUMS_DIR


def _inside_albums(out: Path, p: Path) -> bool:
    try:
        Path(os.path.abspath(p)).relative_to(Path(os.path.abspath(_root(out))))
        return True
    except ValueError:
        return False


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise AlbumError("type an album name")
    if len(name) > 120:
        raise AlbumError("album name is too long (120 characters max)")
    return name


def _folder_for(db: DB, name: str, exclude_id: int | None = None) -> str:
    base = safe_name(name, 80) or "Album"
    taken = {r[0].lower() for r in db.query("SELECT folder FROM albums WHERE id<>?", (exclude_id or -1,))}
    folder, n = base, 2
    while folder.lower() in taken:
        folder = f"{base} ({n})"
        n += 1
    return folder


def list_albums(db: DB) -> list[dict]:
    rows = db.query(
        "SELECT a.id, a.name, a.folder, a.created_at, "
        "(SELECT COUNT(*) FROM album_items i WHERE i.album_id=a.id AND " + VISIBLE_ITEM + ") AS count, "
        "(SELECT i.file_id FROM album_items i WHERE i.album_id=a.id AND " + VISIBLE_ITEM +
        " ORDER BY i.added_at DESC, i.file_id DESC LIMIT 1) AS cover "
        "FROM albums a ORDER BY lower(a.name)")
    return [dict(r) for r in rows]


# photos the Clean up page removed stay in the album table (Undo brings them back) but are not shown
VISIBLE_ITEM = "NOT EXISTS (SELECT 1 FROM file_review fr WHERE fr.file_id=i.file_id AND fr.state='remove')"


def get_album(db: DB, album_id: int) -> dict:
    r = db.one("SELECT id, name, folder, created_at FROM albums WHERE id=?", (album_id,))
    if not r:
        raise AlbumError("album not found")
    return dict(r)


def create_album(db: DB, out: Path, name: str) -> dict:
    name = _clean_name(name)
    if db.one("SELECT 1 FROM albums WHERE lower(name)=lower(?)", (name,)):
        raise AlbumError(f'an album called "{name}" already exists')
    folder = _folder_for(db, name)
    aid = db.execute("INSERT INTO albums(name, folder, created_at) VALUES(?,?,?)", (name, folder, _now())).lastrowid
    db.commit()
    (_root(out) / folder).mkdir(parents=True, exist_ok=True)
    return get_album(db, aid)


def _library_file(db: DB, out: Path, fid: int) -> Path | None:
    r = db.one("SELECT dest_path, rep_id FROM files WHERE id=?", (fid,))
    if not r:
        return None
    for rel in (r["dest_path"],):
        if rel and (Path(out) / rel).exists():
            return Path(out) / rel
    return None


def _make_link(out: Path, src: Path, folder: Path) -> tuple[Path, str]:
    folder.mkdir(parents=True, exist_ok=True)
    stem, suf = src.stem, src.suffix
    target, n = folder / src.name, 1
    while target.exists():
        try:
            if os.path.samefile(target, src):
                return target, "link"
        except OSError:
            pass
        target = folder / f"{stem} ({n}){suf}"
        n += 1
    try:
        os.link(src, target)
        return target, "link"
    except OSError as e:
        log.warning("Albums: hard link not possible for %s (%s); copying instead", src, e)
        tmp = target.with_name(target.name + ".partial")
        shutil.copy2(src, tmp)
        os.replace(tmp, target)
        return target, "copy"


def add_files(db: DB, out: Path, album_id: int, file_ids) -> dict:
    a = get_album(db, album_id)
    folder = _root(out) / a["folder"]
    added = linked = copied = missing = already = 0
    with db.lock:
        for fid in dict.fromkeys(int(x) for x in file_ids):
            if db.one("SELECT 1 FROM album_items WHERE album_id=? AND file_id=?", (album_id, fid)):
                already += 1
                continue
            if not db.one("SELECT 1 FROM files WHERE id=?", (fid,)):
                continue
            src = _library_file(db, out, fid)
            link, method = None, None
            if src is None:
                missing += 1
            else:
                p, method = _make_link(out, src, folder)
                link = str(p.relative_to(out))
                linked += method == "link"
                copied += method == "copy"
            db.execute("INSERT INTO album_items(album_id, file_id, added_at, link_path, method) VALUES(?,?,?,?,?)",
                       (album_id, fid, _now(), link, method))
            added += 1
        db.commit()
    msg = f'Added {added} photo{"s" if added != 1 else ""} to "{a["name"]}"'
    if already:
        msg += f" ({already} already there)"
    if copied:
        msg += f". Hard links were not possible on this drive, so {copied} were copied into the album folder"
    if missing:
        msg += f". {missing} had no copy in your sorted library, so they are in the album but not its folder"
    return {"album": album_id, "added": added, "linked": linked, "copied": copied, "missing": missing,
            "already": already, "message": msg}


def _unlink(out: Path, rel: str | None):
    if not rel:
        return
    p = Path(out) / rel
    if not _inside_albums(out, p) or ".." in Path(rel).parts:
        log.warning("Albums: refusing to remove %s (outside Albums)", p)
        return
    try:
        p.unlink(missing_ok=True)
    except OSError as e:
        log.warning("Albums: could not remove %s: %s", p, e)


def remove_files(db: DB, out: Path, album_id: int, file_ids) -> dict:
    get_album(db, album_id)
    n = 0
    with db.lock:
        for fid in {int(x) for x in file_ids}:
            r = db.one("SELECT link_path FROM album_items WHERE album_id=? AND file_id=?", (album_id, fid))
            if not r:
                continue
            _unlink(out, r[0])
            db.execute("DELETE FROM album_items WHERE album_id=? AND file_id=?", (album_id, fid))
            n += 1
        db.commit()
    return {"removed": n}


def rename_album(db: DB, out: Path, album_id: int, name: str) -> dict:
    a = get_album(db, album_id)
    name = _clean_name(name)
    if db.one("SELECT 1 FROM albums WHERE lower(name)=lower(?) AND id<>?", (name, album_id)):
        raise AlbumError(f'an album called "{name}" already exists')
    folder = _folder_for(db, name, exclude_id=album_id)
    old, new = _root(out) / a["folder"], _root(out) / folder
    with db.lock:
        if folder != a["folder"]:
            if old.is_dir() and not new.exists():
                os.rename(old, new)
            elif old.is_dir() and old.resolve() != new.resolve():
                # case-only rename on Windows, or a stray folder in the way
                tmp = old.with_name(old.name + ".renaming")
                os.rename(old, tmp)
                os.rename(tmp, new)
            for r in db.query("SELECT file_id, link_path FROM album_items WHERE album_id=? AND link_path IS NOT NULL",
                              (album_id,)):
                rel = Path(r["link_path"])
                db.execute("UPDATE album_items SET link_path=? WHERE album_id=? AND file_id=?",
                           (str(Path(ALBUMS_DIR) / folder / rel.name), album_id, r["file_id"]))
        db.execute("UPDATE albums SET name=?, folder=? WHERE id=?", (name, folder, album_id))
        db.commit()
    return get_album(db, album_id)


def delete_album(db: DB, out: Path, album_id: int) -> dict:
    """Delete the album and its folder of links. Photos in the library are never deleted."""
    a = get_album(db, album_id)
    with db.lock:
        rows = db.query("SELECT link_path FROM album_items WHERE album_id=?", (album_id,))
        for r in rows:
            _unlink(out, r[0])
        d = _root(out) / a["folder"]
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
        db.execute("DELETE FROM album_items WHERE album_id=?", (album_id,))
        db.execute("DELETE FROM albums WHERE id=?", (album_id,))
        db.commit()
    return {"deleted": album_id, "photos_kept": len(rows), "folder_left": d.is_dir()}


def drop_links_to(db: DB, out: Path, file_id: int):
    """A library copy is being removed by Clean up: drop its album links so the space is really freed.
    The album rows stay, so Undo (or a later sync) can bring the links back."""
    for r in db.query("SELECT album_id, link_path FROM album_items WHERE file_id=? AND link_path IS NOT NULL",
                      (file_id,)):
        _unlink(out, r["link_path"])
        db.execute("UPDATE album_items SET link_path=NULL, method=NULL WHERE album_id=? AND file_id=?",
                   (r["album_id"], file_id))


def sync_folders(db: DB, out: Path) -> dict:
    """Make every album folder match the table: links to removed photos go, missing/stale links come back."""
    made = dropped = 0
    with db.lock:
        for a in db.query("SELECT id, folder FROM albums"):
            folder = _root(out) / a["folder"]
            for r in db.query("SELECT i.file_id, i.link_path, fr.state FROM album_items i "
                              "LEFT JOIN file_review fr ON fr.file_id=i.file_id WHERE i.album_id=?", (a["id"],)):
                src = _library_file(db, out, r["file_id"]) if r["state"] != "remove" else None
                link = Path(out) / r["link_path"] if r["link_path"] else None
                if src is None:
                    if link is not None:
                        _unlink(out, r["link_path"])
                        db.execute("UPDATE album_items SET link_path=NULL, method=NULL WHERE album_id=? AND file_id=?",
                                   (a["id"], r["file_id"]))
                        dropped += 1
                    continue
                ok = False
                if link is not None and link.exists():
                    try:
                        ok = os.path.samefile(link, src) or (
                            link.stat().st_size == src.stat().st_size and link.name.startswith(src.stem))
                    except OSError:
                        ok = False
                    if not ok:
                        _unlink(out, r["link_path"])
                if not ok:
                    p, method = _make_link(out, src, folder)
                    db.execute("UPDATE album_items SET link_path=?, method=? WHERE album_id=? AND file_id=?",
                               (str(p.relative_to(out)), method, a["id"], r["file_id"]))
                    made += 1
        db.commit()
    return {"made": made, "dropped": dropped}
