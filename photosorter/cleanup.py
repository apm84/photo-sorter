"""Duplicate clean-up: apply the user's keep/remove decisions to the ORGANIZED OUTPUT COPIES only.

Safety rules (enforced here, covered by tests):
- Only paths inside the output folder are ever moved or removed. Original source files are never touched.
- Removed copies go to the Windows Recycle Bin (send2trash). If that is unavailable or fails, they are moved to
  <output>/_index/_trash/ instead. Nothing is permanently deleted.
- Every action is logged in SQLite (review_actions) with what was done, so the last action can be undone.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from . import albums
from .config import INDEX_DIRNAME, index_dir
from .db import DB
from .dedupe import quality_key
from .pipeline import build_people_folders, in_main_tree, main_dest, unique_dest, write_reports

log = logging.getLogger("photosorter")

TRASH_DIRNAME = "_trash"


class CleanupError(Exception):
    pass


# ---- file helpers ---------------------------------------------------------------------------------------
def _inside(out: Path, p: Path) -> bool:
    try:
        Path(os.path.abspath(p)).relative_to(Path(os.path.abspath(out)))
        return True
    except ValueError:
        return False


def _guard(out: Path, rel: str, src_path: str | None) -> Path:
    """Absolute path for an output-relative path, refusing anything outside the output or equal to an original."""
    if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise CleanupError(f"refusing unsafe path {rel!r}")
    p = Path(out) / rel
    if not _inside(out, p) or Path(rel).parts[0].lower() == INDEX_DIRNAME.lower():
        raise CleanupError(f"refusing path outside the sorted library: {p}")
    if src_path:
        if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(src_path)):
            raise CleanupError(f"refusing to touch an original file: {p}")
        try:
            if p.exists() and Path(src_path).exists() and os.path.samefile(p, src_path):
                raise CleanupError(f"refusing to touch an original file (same file): {p}")
        except OSError:
            pass
    return p


def recycle_available() -> bool:
    if os.environ.get("PHOTOSORTER_TRASH") == "folder":
        return False
    try:
        import send2trash  # noqa: F401
        return True
    except Exception:
        return False


def remove_copy(out: Path, p: Path, action_id: int) -> dict:
    """Recycle Bin first, else move into _index/_trash/<action>/. Returns how it was removed."""
    if recycle_available():
        try:
            from send2trash import send2trash

            send2trash(str(p))
            return {"method": "recycle"}
        except Exception as e:
            log.warning("Recycle Bin failed for %s (%s); using _index/_trash", p, e)
    rel = p.relative_to(out)
    t = Path(out) / INDEX_DIRNAME / TRASH_DIRNAME / f"action-{action_id:05d}" / rel
    t.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(p), str(t))
    return {"method": "trash", "trash_path": str(t.relative_to(out))}


def _drop_people_links(out: Path, p: Path):
    """People\\<Name>\\ holds hard links to library photos; drop links to a removed copy so space is freed."""
    people = Path(out) / "People"
    if not people.is_dir() or not p.exists():
        return
    for link in people.glob(f"*/{p.name}"):
        try:
            if os.path.samefile(link, p):
                link.unlink()
        except OSError:
            pass


def _tidy_dup_folder(out: Path, rel: str | None):
    """Remove an emptied Duplicates\\group-N folder (only its _kept.txt note left)."""
    if not rel or Path(rel).parts[0].lower() != "duplicates":
        return
    d = (Path(out) / rel).parent
    try:
        left = [x for x in d.iterdir() if x.name != "_kept.txt"]
        if not left:
            (d / "_kept.txt").unlink(missing_ok=True)
            d.rmdir()
    except OSError:
        pass


# ---- reading groups -------------------------------------------------------------------------------------
REASONS = [
    (0, "higher resolution"),
    (1, "has the original camera date"),
    (2, "has camera info"),
    (3, "has GPS location"),
    (4, "larger file (less compressed)"),
    (5, "the indexed original of identical copies"),
    (6, "shorter, original-looking path"),
]


def keeper_reasons(keeper: dict, others: list[dict]) -> list[str]:
    """Plain-language reasons the keeper won (first quality_key component where it beats each other copy)."""
    out = []
    kk = quality_key(keeper)
    for o in others:
        ok = quality_key(o)
        for i, label in REASONS:
            if kk[i] != ok[i]:
                if kk[i] > ok[i]:
                    if i == 0:
                        label = (f"higher resolution ({keeper.get('width')}x{keeper.get('height')} vs "
                                 f"{o.get('width')}x{o.get('height')})")
                    if label not in out:
                        out.append(label)
                break
        else:
            if "identical quality (first one found kept)" not in out:
                out.append("identical quality (first one found kept)")
    return out


MEMBER_COLS = ("id, src_path, filename, size, width, height, taken_at, date_source, camera_make, camera_model, "
               "lat, lon, place, dest_path, organized, sha256, rep_id, type, is_keeper, analyzed, mtime, ext, software")


def group_detail(db: DB, gid: int, out: Path | None = None) -> dict:
    g = db.one("SELECT * FROM dup_groups WHERE id=?", (gid,))
    if not g:
        raise CleanupError(f"no duplicate group {gid}")
    members = [dict(r) for r in db.query(
        f"SELECT {MEMBER_COLS}, fr.state AS review FROM files f LEFT JOIN file_review fr ON fr.file_id=f.id "
        "WHERE f.dup_group=? ORDER BY f.id", (gid,))]
    keeper = g["keeper_id"]
    rec = g["suggested_keeper_id"] or keeper
    byid = {m["id"]: m for m in members}
    reviewed = all(m["review"] for m in members) and not g["suggested_keeper_id"]
    for m in members:
        m["location"] = ("library" if in_main_tree(m["dest_path"]) else
                         "duplicates" if m["dest_path"] else "not copied")
        m["exact_of_keeper"] = m["sha256"] == byid.get(keeper, {}).get("sha256") and m["id"] != keeper
        if reviewed:
            m["proposed"] = m["review"]
        elif m["id"] == rec or (m["review"] == "keep" and m["id"] != keeper):
            m["proposed"] = "keep"
        else:
            m["proposed"] = "remove"
    learned = None
    if not reviewed and not g["suggested_keeper_id"] and len(members) > 1:
        learned = _learned_choice(db, members, rec, out)
        if learned and learned["id"] != rec:
            rec = learned["id"]
            for m in members:
                if m["review"] is None:
                    m["proposed"] = "keep" if m["id"] == rec else "remove"
    rk = byid.get(rec)
    reasons = keeper_reasons(rk, [m for m in members if m["id"] != rec]) if rk else []
    if learned and learned.get("why"):
        reasons.insert(0, f"Learned from your choices: you usually keep {learned['why']}")
    if g["suggested_keeper_id"]:
        reasons.insert(0, "a better copy arrived in a later batch (your library still has the old keeper)")
    return {"id": gid, "kind": g["kind"], "keeper_id": keeper, "recommended_keeper_id": rec,
            "suggested_keeper_id": g["suggested_keeper_id"], "note": g["note"], "reviewed": reviewed,
            "reasons": reasons, "members": members}


def _learned_choice(db: DB, members: list[dict], current: int | None, out: Path | None) -> dict | None:
    """v0.4: the copy the learned preference model would keep, if it has learned enough and is fairly sure."""
    from . import prefs

    model = prefs.load_model(db)
    if not model.active:
        return None
    cands = [m for m in members if m["review"] != "remove"] or members
    if len(cands) < 2:
        return None
    best, feats, _ = model.rank(cands, out)
    if current in feats and best != current:
        if model.prob(feats[best], feats[current]) < prefs.SWITCH_P:
            best = current
    other = current if best != current and current in feats else \
        next(m["id"] for m in cands if m["id"] != best)
    return {"id": best, "why": model.why(feats[best], feats[other])}


def pending_group_ids(db: DB) -> list[int]:
    return [r[0] for r in db.query(
        "SELECT g.id FROM dup_groups g WHERE g.suggested_keeper_id IS NOT NULL OR EXISTS ("
        "SELECT 1 FROM files f LEFT JOIN file_review fr ON fr.file_id=f.id WHERE f.dup_group=g.id AND fr.state IS NULL"
        ") ORDER BY (g.suggested_keeper_id IS NULL), g.id")]


def summary(db: DB) -> dict:
    total = db.one("SELECT COUNT(*) FROM dup_groups")[0]
    pending = pending_group_ids(db)
    last = db.one("SELECT id, label, created_at FROM review_actions WHERE undone=0 ORDER BY id DESC LIMIT 1")
    return {"total": total, "reviewed": total - len(pending), "pending": len(pending),
            "needs_review": db.one("SELECT COUNT(*) FROM dup_groups WHERE suggested_keeper_id IS NOT NULL")[0],
            "recycle_bin": recycle_available(), "batch_running": batch_running(db),
            "last_action": dict(last) if last else None}


# ---- applying decisions ---------------------------------------------------------------------------------
class _Action:
    def __init__(self, db: DB, out: Path, label: str):
        self.db, self.out, self.label = db, Path(out), label
        self.ops: list[dict] = []
        self.prev_files: dict[int, dict] = {}
        self.prev_groups: dict[int, dict] = {}
        self.groups: list[int] = []
        now = datetime.now().isoformat(timespec="seconds")
        self.id = db.execute("INSERT INTO review_actions(created_at, label, groups, ops, prev) VALUES(?,?,?,?,?)",
                             (now, label, "[]", "[]", "{}")).lastrowid
        self.used = {r[0].lower() for r in db.query("SELECT dest_path FROM files WHERE dest_path IS NOT NULL")}

    def remember_file(self, fid: int):
        if fid not in self.prev_files:
            r = self.db.one("SELECT f.dest_path, f.is_keeper, f.organized, fr.state, fr.action_id, fr.decided_at "
                            "FROM files f LEFT JOIN file_review fr ON fr.file_id=f.id WHERE f.id=?", (fid,))
            self.prev_files[fid] = dict(r)

    def remember_group(self, gid: int):
        if gid not in self.prev_groups:
            r = self.db.one("SELECT keeper_id, suggested_keeper_id, note FROM dup_groups WHERE id=?", (gid,))
            self.prev_groups[gid] = dict(r)

    def save(self):
        self.db.execute("UPDATE review_actions SET groups=?, ops=?, prev=? WHERE id=?",
                        (json.dumps(self.groups), json.dumps(self.ops),
                         json.dumps({"files": self.prev_files, "groups": self.prev_groups}), self.id))


def _apply_group(act: _Action, gid: int, keep_ids: set[int], keeper_id: int | None, allow_empty: bool = False):
    db, out = act.db, act.out
    g = db.one("SELECT * FROM dup_groups WHERE id=?", (gid,))
    if not g:
        raise CleanupError(f"no duplicate group {gid}")
    members = {r["id"]: dict(r) for r in db.query(f"SELECT {MEMBER_COLS} FROM files WHERE dup_group=?", (gid,))}
    keep_ids = set(keep_ids)
    if not keep_ids:
        if not allow_empty:
            raise CleanupError("keep at least one photo in each group")
        keeper_id = None
    elif not keep_ids <= set(members) or keeper_id not in keep_ids:
        raise CleanupError("decision does not match the group's photos (re-load the page)")
    act.remember_group(gid)
    act.groups.append(gid)
    old_keeper = members.get(g["keeper_id"])
    # folder the main keeper should live in: the old keeper's library folder (keeps earlier albums consistent)
    keeper_folder = None
    if old_keeper and in_main_tree(old_keeper["dest_path"]):
        keeper_folder = Path(old_keeper["dest_path"]).parent
    now = datetime.now().isoformat(timespec="seconds")

    # 1) remove the copies marked duplicate (output copies only; never-copied exact dupes are just marked)
    for fid in sorted(set(members) - keep_ids):
        m = members[fid]
        act.remember_file(fid)
        if m["dest_path"]:
            p = _guard(out, m["dest_path"], m["src_path"])
            if p.exists():
                _drop_people_links(out, p)
                albums.drop_links_to(db, out, fid)
                size = p.stat().st_size
                how = remove_copy(out, p, act.id)
                act.ops.append({"op": "remove", "file_id": fid, "path": m["dest_path"], "size": size, **how})
                _tidy_dup_folder(out, m["dest_path"])
            act.used.discard(m["dest_path"].lower())
            m["dest_path"] = None
        db.execute("UPDATE files SET dest_path=NULL, organized=1, is_keeper=0 WHERE id=?", (fid,))
        db.execute("INSERT OR REPLACE INTO file_review(file_id, state, action_id, decided_at) VALUES(?,?,?,?)",
                   (fid, "remove", act.id, now))

    # 2) promote kept photos into the library (move from Duplicates, or copy the original if never copied)
    for fid in sorted(keep_ids, key=lambda f: f != keeper_id):
        m = members[fid]
        act.remember_file(fid)
        if not in_main_tree(m["dest_path"]):
            # Already have this exact file in the library: mark kept, don't copy a second identical file.
            identical = next((o for o in members.values()
                              if o["id"] != fid and o["id"] in keep_ids
                              and o.get("sha256") and o["sha256"] == m.get("sha256")
                              and in_main_tree(o.get("dest_path"))), None)
            if identical and not m["dest_path"]:
                db.execute("UPDATE files SET organized=1, is_keeper=1 WHERE id=?", (fid,))
                db.execute("INSERT OR REPLACE INTO file_review(file_id, state, action_id, decided_at) VALUES(?,?,?,?)",
                           (fid, "keep", act.id, now))
                continue
            rel = (keeper_folder / m["filename"]) if (fid == keeper_id and keeper_folder) else main_dest(m)
            target = unique_dest(out, rel, None, act.used)
            _guard(out, str(target.relative_to(out)), m["src_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            cur = out / m["dest_path"] if m["dest_path"] else None
            if cur is not None and cur.exists():
                _guard(out, m["dest_path"], m["src_path"])
                shutil.move(str(cur), str(target))
                act.ops.append({"op": "move", "file_id": fid, "from": m["dest_path"],
                                "to": str(target.relative_to(out))})
                _tidy_dup_folder(out, m["dest_path"])
            else:
                if not Path(m["src_path"]).exists():
                    raise CleanupError(f"original not found, cannot copy it into the library: {m['src_path']}")
                tmp = target.with_name(target.name + ".partial")
                shutil.copy2(m["src_path"], tmp)       # read-only use of the original
                os.replace(tmp, target)
                act.ops.append({"op": "copy", "file_id": fid, "to": str(target.relative_to(out))})
            newrel = str(target.relative_to(out))
            act.used.add(newrel.lower())
            m["dest_path"] = newrel
            db.execute("UPDATE files SET dest_path=?, organized=1 WHERE id=?", (newrel, fid))
        db.execute("UPDATE files SET is_keeper=1 WHERE id=?", (fid,))
        db.execute("INSERT OR REPLACE INTO file_review(file_id, state, action_id, decided_at) VALUES(?,?,?,?)",
                   (fid, "keep", act.id, now))

    db.execute("UPDATE dup_groups SET keeper_id=?, suggested_keeper_id=NULL, note=NULL WHERE id=?", (keeper_id, gid))


def batch_running(db: DB) -> bool:
    import time

    hb = db.get_meta("pipeline_heartbeat")
    return bool(hb) and time.time() - float(hb) < 90


def _check_idle(db: DB):
    if batch_running(db):
        raise CleanupError("A sorting batch is running. Wait for it to finish, then clean up.")


def apply_decision(db: DB, out: Path, gid: int, keep_ids, keeper_id: int | None = None,
                   allow_empty: bool = False) -> dict:
    _check_idle(db)
    keep_ids = {int(x) for x in keep_ids}
    if keeper_id is None:
        g = db.one("SELECT keeper_id, suggested_keeper_id FROM dup_groups WHERE id=?", (gid,))
        pref = [x for x in (g and (g["suggested_keeper_id"], g["keeper_id"]) or ()) if x in keep_ids]
        keeper_id = pref[0] if pref else min(keep_ids) if keep_ids else None
    with db.lock:
        act = _Action(db, out, f"group {gid}" if keep_ids else f"delete all in group {gid}")
        members = [dict(r) for r in db.query(f"SELECT {MEMBER_COLS} FROM files WHERE dup_group=?", (gid,))]
        try:
            _apply_group(act, gid, keep_ids, keeper_id, allow_empty=allow_empty)
        except Exception:
            act.save(); db.commit()
            _rollback_partial(db, out, act)
            raise
        act.save()
        if keep_ids and not allow_empty:
            # v0.4 learning: only real choices (some kept, some removed); Keep all / Delete all teach nothing
            from . import prefs
            try:
                prefs.record_decision(db, act.id, gid, members, keep_ids, Path(out))
            except Exception:
                pass
        db.commit()
        write_reports(db, out)
        _sync_albums(db, out)
    return {"action_id": act.id, "ops": len(act.ops)}


def delete_group(db: DB, out: Path, gid: int) -> dict:
    """Remove every output copy in the group and keep none. Originals are never touched."""
    return apply_decision(db, out, gid, [], None, allow_empty=True)


def approve_all(db: DB, out: Path) -> dict:
    """Apply the recommendation (keep the recommended keeper, plus anything already kept) to every pending group."""
    _check_idle(db)
    ids = pending_group_ids(db)
    with db.lock:
        act = _Action(db, out, f"approve all ({len(ids)} groups)")
        done, errors = 0, []
        for gid in ids:
            d = group_detail(db, gid, out)
            keep = {m["id"] for m in d["members"] if m["proposed"] == "keep"}
            try:
                _apply_group(act, gid, keep, d["recommended_keeper_id"])
                done += 1
            except (CleanupError, OSError) as e:
                errors.append(f"group {gid}: {e}")
        act.save()
        db.commit()
        write_reports(db, out)
        _sync_albums(db, out)
    return {"action_id": act.id, "groups": done, "errors": errors}


def _rollback_partial(db: DB, out: Path, act: _Action):
    try:
        undo_action(db, out, act.id)
    except Exception:
        log.exception("could not roll back partial clean-up action %s", act.id)


def undo_last(db: DB, out: Path) -> dict:
    _check_idle(db)
    r = db.one("SELECT id FROM review_actions WHERE undone=0 ORDER BY id DESC LIMIT 1")
    if not r:
        raise CleanupError("nothing to undo")
    return undo_action(db, out, r[0])


def undo_action(db: DB, out: Path, action_id: int) -> dict:
    out = Path(out)
    with db.lock:
        a = db.one("SELECT * FROM review_actions WHERE id=?", (action_id,))
        if not a or a["undone"]:
            raise CleanupError("nothing to undo")
        ops = json.loads(a["ops"])
        prev = json.loads(a["prev"] or "{}")
        problems = []
        for op in reversed(ops):
            fid = op["file_id"]
            src = (db.one("SELECT src_path FROM files WHERE id=?", (fid,)) or [None])[0]
            try:
                if op["op"] == "move":
                    cur = _guard(out, op["to"], src)
                    back = _guard(out, op["from"], src)
                    back.parent.mkdir(parents=True, exist_ok=True)
                    if back.exists():
                        raise CleanupError(f"{back} is in the way")
                    shutil.move(str(cur), str(back))
                    _tidy_dup_folder(out, op["to"])
                elif op["op"] == "copy":
                    p = _guard(out, op["to"], src)
                    if p.exists():
                        p.unlink()       # our own copy of an original that is still in place
                    _tidy_dup_folder(out, op["to"])
                elif op["op"] == "remove":
                    back = _guard(out, op["path"], src)
                    back.parent.mkdir(parents=True, exist_ok=True)
                    t = out / op["trash_path"] if op.get("trash_path") else None
                    if back.exists():
                        pass
                    elif t is not None and t.exists() and _inside(out, t):
                        shutil.move(str(t), str(back))
                    elif src and Path(src).exists():
                        tmp = back.with_name(back.name + ".partial")
                        shutil.copy2(src, tmp)   # recycled: restore from the untouched original instead
                        os.replace(tmp, back)
                    else:
                        raise CleanupError(f"cannot restore {back}: original missing")
            except (CleanupError, OSError) as e:
                problems.append(str(e))
        for fid, f in prev.get("files", {}).items():
            db.execute("UPDATE files SET dest_path=?, is_keeper=?, organized=? WHERE id=?",
                       (f["dest_path"], f["is_keeper"], f["organized"], int(fid)))
            if f["state"]:
                db.execute("INSERT OR REPLACE INTO file_review(file_id, state, action_id, decided_at) VALUES(?,?,?,?)",
                           (int(fid), f["state"], f["action_id"], f["decided_at"]))
            else:
                db.execute("DELETE FROM file_review WHERE file_id=?", (int(fid),))
        for gid, g in prev.get("groups", {}).items():
            db.execute("UPDATE dup_groups SET keeper_id=?, suggested_keeper_id=?, note=? WHERE id=?",
                       (g["keeper_id"], g["suggested_keeper_id"], g["note"], int(gid)))
        db.execute("UPDATE review_actions SET undone=1 WHERE id=?", (action_id,))
        from .prefs import forget_action
        forget_action(db, action_id)            # an undone choice is not a preference
        db.commit()
        write_reports(db, out)
        build_people_folders(out, db)
        _sync_albums(db, out)
    return {"undone": action_id, "label": a["label"], "problems": problems}


def _sync_albums(db: DB, out: Path):
    try:
        albums.sync_folders(db, out)
    except Exception:
        log.exception("could not sync album folders")


def preview_all(db: DB) -> dict:
    """What 'approve all remaining' would do, for the confirmation dialog."""
    groups = files = copies = promote = 0
    nbytes = 0
    for gid in pending_group_ids(db):
        d = group_detail(db, gid)
        groups += 1
        for m in d["members"]:
            if m["proposed"] == "remove":
                files += 1
                if m["dest_path"]:
                    copies += 1
                    nbytes += m["size"] or 0
            elif m["location"] != "library":
                promote += 1
    return {"groups": groups, "remove": files, "copies_to_remove": copies, "promote": promote,
            "bytes_to_free": nbytes, "recycle_bin": recycle_available()}


# ---- clean-up tasks (v0.3) ------------------------------------------------------------------------------
def trash_dir(out: Path) -> Path:
    return Path(out) / INDEX_DIRNAME / TRASH_DIRNAME


def _dir_stats(d: Path) -> tuple[int, int]:
    n = b = 0
    if d.is_dir():
        for p in d.rglob("*"):
            try:
                if p.is_file():
                    n += 1
                    b += p.stat().st_size
            except OSError:
                pass
    return n, b


def recycled_stats(db: DB) -> tuple[int, int]:
    """Copies this app sent to the Windows Recycle Bin (actions not undone). Sizes from 0.3 on are exact;
    older actions use the size of the original file."""
    n = b = 0
    for a in db.query("SELECT ops FROM review_actions WHERE undone=0"):
        for op in json.loads(a["ops"] or "[]"):
            if op.get("op") == "remove" and op.get("method") == "recycle":
                n += 1
                size = op.get("size")
                if size is None:
                    r = db.one("SELECT size FROM files WHERE id=?", (op["file_id"],))
                    size = r[0] if r and r[0] else 0
                b += size
    return n, b


def tasks(db: DB, out: Path) -> dict:
    """Suggested clean-up actions, each with a file count and the space involved."""
    out = Path(out)
    pv = preview_all(db)
    t = []
    t.append({"id": "review", "title": "Review duplicate groups",
              "detail": (f"{pv['groups']} group{'s are' if pv['groups'] != 1 else ' is'} waiting. Approving my picks "
                         f"would remove {pv['copies_to_remove']} cop{'ies' if pv['copies_to_remove'] != 1 else 'y'} "
                         f"from your sorted library.") if pv["groups"] else "Every duplicate group has been reviewed.",
              "files": pv["copies_to_remove"], "bytes": pv["bytes_to_free"],
              "action": {"kind": "tab", "label": "Review groups", "target": "review"} if pv["groups"] else None,
              "done": not pv["groups"]})
    tn, tb = _dir_stats(trash_dir(out))
    t.append({"id": "trash", "title": "Empty the app's trash folder",
              "detail": ("Copies removed while the Recycle Bin was not available are kept in "
                         f"{INDEX_DIRNAME}\\{TRASH_DIRNAME}. Emptying it " +
                         ("sends the folder to the Recycle Bin." if recycle_available() else
                          "deletes them permanently (Undo can still restore them from your originals).")) if tn else
                        f"{INDEX_DIRNAME}\\{TRASH_DIRNAME} is empty.",
              "files": tn, "bytes": tb, "done": not tn,
              "action": {"kind": "button", "label": "Empty...", "api": "/api/cleanup/empty-trash",
                         "confirm": "recycle" if recycle_available() else "permanent"} if tn else None})
    rn, rb = recycled_stats(db)
    t.append({"id": "recycle", "title": "Empty the Windows Recycle Bin",
              "detail": (f"This app sent {rn} copies to the Recycle Bin. They use space until you empty it "
                         "(right-click Recycle Bin on the desktop, then Empty Recycle Bin). The app can't empty it "
                         "for you. Your originals are not affected.") if rn else "Nothing from this app is in the Recycle Bin.",
              "files": rn, "bytes": rb, "done": not rn, "info": True,
              "action": {"kind": "button", "label": "Open Recycle Bin", "api": "/api/open-recycle-bin"} if rn else None})
    ex = db.one(
        "SELECT COUNT(*), COALESCE(SUM(f.size),0) FROM files f WHERE f.organized=1 AND f.dest_path IS NULL AND "
        "f.analyzed=1 AND NOT EXISTS (SELECT 1 FROM file_review fr WHERE fr.file_id=f.id AND fr.state='keep') AND "
        "((f.rep_id IS NOT NULL AND f.rep_id<>f.id) OR f.sha256 IN (SELECT k.sha256 FROM dup_groups g JOIN files k "
        "ON k.id=g.keeper_id WHERE g.id=f.dup_group))")
    t.append({"id": "exact", "title": "Identical copies that were not copied",
              "detail": ("Byte-identical copies of photos already in your library were not copied, saving space. "
                         "Nothing to do; the list is in _index\\reports\\exact-duplicates.csv.") if ex[0] else
                        "No identical copies were skipped.",
              "files": ex[0], "bytes": ex[1], "done": True, "info": True,
              "action": {"kind": "button", "label": "Show list", "api": "/api/cleanup/open-report/exact-duplicates"}
              if ex[0] and (index_dir(out) / "reports" / "exact-duplicates.csv").exists() else None})
    un = db.one("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE analyzed=-1")
    t.append({"id": "unreadable", "title": "Files that could not be read",
              "detail": ("These were copied to Other\\ as they are. The list with a reason for each is in "
                         "_index\\reports\\unreadable.csv (common reasons: videos with a photo extension, damaged or "
                         "cut-off files, HEIC files without HEIC support).") if un[0] else "Every file could be read.",
              "files": un[0], "bytes": un[1], "done": not un[0], "info": True,
              "action": {"kind": "button", "label": "Show list", "api": "/api/cleanup/open-report/unreadable"}
              if un[0] and (index_dir(out) / "reports" / "unreadable.csv").exists() else None})
    lq = db.one("SELECT COUNT(*) FROM faces WHERE low_quality=1")[0]
    if lq:
        t.append({"id": "faces", "title": "Low-quality faces skipped for grouping",
                  "detail": "Tiny, blurry or uncertain faces are kept but not used to build face groups. They only "
                            "join a named person on a very confident match. Nothing to do.",
                  "files": lq, "bytes": None, "done": True, "info": True, "unit": "faces",
                  "action": {"kind": "link", "label": "Face groups", "href": "/people#settings"}})
    return {"tasks": t, "recycle_bin": recycle_available()}


def empty_trash(db: DB, out: Path) -> dict:
    """Empty <output>/_index/_trash only: Recycle Bin when available, else permanently."""
    d = trash_dir(out)
    if not d.is_dir():
        return {"files": 0, "bytes": 0, "method": None}
    if d.resolve() != (Path(out).resolve() / INDEX_DIRNAME / TRASH_DIRNAME) or d.is_symlink():
        raise CleanupError("refusing: unexpected trash folder")
    n, b = _dir_stats(d)
    if recycle_available():
        try:
            from send2trash import send2trash

            send2trash(str(d))
            return {"files": n, "bytes": b, "method": "recycle"}
        except Exception as e:
            log.warning("Recycle Bin failed for %s (%s); deleting permanently", d, e)
    shutil.rmtree(d)
    return {"files": n, "bytes": b, "method": "deleted"}
