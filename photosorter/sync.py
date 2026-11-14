"""Family sync (v0.4): one-way copy of the family-facing library from this PC to the family calendar Pi.

The PC stays the master. Nothing on the PC is ever modified: originals and sorted copies are only read, and
the only files written are this module's own state under <output>/_index (sync.json, sync-state.json,
sync-control.json, sync-cache.sqlite, sync-cache/ face crops) plus the normal thumbnail cache.

What goes to the Pi (receiver: family-calendar /api/photosync/*, LAN only, pairing token):
  * a catalog (gzip NDJSON): photos with date, size, place, people and albums; named people; albums
  * thumbnails (400 px) and previews (1600 px) from the existing _index/thumbs cache
  * optionally the full-size sorted copies (Settings: "Also send full-size photos")
  * one face picture per named person
Order: catalog, faces, thumbnails, previews, then full-size, so the family app is useful early.

Transfer: HTTP(S) POSTs of <= 1 MB chunks with offsets, so an interrupted upload resumes where it stopped; the
Pi checks every finished file's SHA-256. Only missing files are sent; files the catalog no longer mentions are
deleted on the Pi at the end (deletions propagate). Speed is capped by a token bucket (default 2 MB/s), an
optional time window (e.g. 23:00-06:00) limits when it runs, and Pause stops between chunks.

The worker is a separate process (`python -m photosorter sync --out <folder>`), started by "Sync now", after
each sort run, or at Windows sign-in (Install-Sync-Autostart.bat). It runs at below-normal priority and keeps
going when the browser or the Photo Sorter window is closed. One worker per output folder (lock file).

The token is never logged or shown again; it's kept per output folder in %LOCALAPPDATA%\\PhotoSorter.
The client refuses to send it to anything but a private (home network) address.
"""
from __future__ import annotations

import gzip
import hashlib
import http.client
import io
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import __version__, background
from .config import app_data_dir, db_path, index_dir

log = logging.getLogger("photosorter.sync")

PROTOCOL = 1
CHUNK = 1 << 20
DEFAULTS = {
    "pi_url": "",                  # e.g. https://calendar.example.org (the calendar's own address)
    "enabled": True,               # sync on a schedule / after runs once set up
    "max_mbps": 2.0,               # megabytes per second (Pi on slow Wi-Fi)
    "window_enabled": False,       # only sync inside a time window
    "window_start": "23:00",
    "window_end": "06:00",
    "include_full": True,          # also send full-size photos (largest part; previews go first)
    "include_screenshots": False,  # photos only by default; albums are always included
    "interval_minutes": 60,        # look for changes this often while the worker runs
    "auto_after_run": True,        # start a sync after each sort run
    "verify_tls": True,
}
NAME_RE = re.compile(r"^[0-9a-f]{64}\.(t|p|o|f|catalog)\.[a-z0-9]{1,8}(\.gz)?$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{20,200}$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EST_BYTES = {"t": 40_000, "p": 450_000, "f": 15_000}
FACE_SIDE = 240


# ---- files ------------------------------------------------------------------------------------------------
def _key(out) -> str:
    return os.path.normcase(os.path.abspath(str(out)))


def _p(out, name: str) -> Path:
    return index_dir(Path(out)) / name


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(p: Path, data) -> None:
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    for _ in range(10):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:          # Windows: a reader has it open for a moment
            time.sleep(0.05)
    os.replace(tmp, p)


def load_config(out) -> dict:
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in _read_json(_p(out, "sync.json"), {}).items() if k in DEFAULTS})
    return cfg


def _secrets_path() -> Path:
    return app_data_dir() / "sync-secrets.json"


def get_token(out) -> str:
    return _read_json(_secrets_path(), {}).get(_key(out), "")


def set_token(out, token: str | None) -> None:
    p = _secrets_path()
    data = _read_json(p, {})
    if token:
        data[_key(out)] = token
    else:
        data.pop(_key(out), None)
    _write_json(p, data)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def update_config(out, body: dict) -> dict:
    """Validate and save settings from the Sync page. A token is only replaced when a new one is given."""
    cfg = load_config(out)
    if "pi_url" in body:
        url = str(body["pi_url"] or "").strip().rstrip("/")
        if url:
            u = urlsplit(url)
            if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
                raise ValueError("Enter the calendar's address, like https://calendar.example.org or http://192.168.1.50:8080")
            url = f"{u.scheme}://{u.netloc}"
        cfg["pi_url"] = url
    for k in ("enabled", "window_enabled", "include_full", "include_screenshots", "auto_after_run", "verify_tls"):
        if k in body:
            cfg[k] = bool(body[k])
    if "max_mbps" in body:
        v = float(body["max_mbps"])
        if not 0.1 <= v <= 100:
            raise ValueError("Speed limit must be between 0.1 and 100 MB/s")
        cfg["max_mbps"] = round(v, 2)
    for k in ("window_start", "window_end"):
        if k in body:
            if not HHMM_RE.match(str(body[k])):
                raise ValueError("Times must look like 23:00")
            cfg[k] = str(body[k])
    if "interval_minutes" in body:
        v = int(body["interval_minutes"])
        if not 5 <= v <= 1440:
            raise ValueError("Check every 5 to 1440 minutes")
        cfg["interval_minutes"] = v
    if body.get("clear_token"):
        set_token(out, None)
    elif body.get("token"):
        tok = str(body["token"]).strip()
        if not TOKEN_RE.match(tok):
            raise ValueError("That pairing code doesn't look right. Copy it again from the calendar admin.")
        set_token(out, tok)
    _write_json(_p(out, "sync.json"), cfg)
    return status(out)


# ---- control + state (shared between the web page and the worker process) ------------------------------------
def read_control(out) -> dict:
    return _read_json(_p(out, "sync-control.json"), {"paused": False, "now": 0})


def _update_control(out, **kw) -> dict:
    c = read_control(out)
    c.update(kw)
    _write_json(_p(out, "sync-control.json"), c)
    return c


def set_paused(out, paused: bool) -> dict:
    _update_control(out, paused=bool(paused))
    return status(out)


def read_state(out) -> dict:
    return _read_json(_p(out, "sync-state.json"), {})


def configured(out) -> bool:
    return bool(load_config(out)["pi_url"] and get_token(out))


def status(out) -> dict:
    cfg = load_config(out)
    st = read_state(out)
    ctrl = read_control(out)
    running = worker_alive(out)
    return {"config": cfg, "paired": bool(get_token(out)), "configured": configured(out), "state": st,
            "paused": bool(ctrl.get("paused")), "worker_running": running, "autostart": autostart_installed(),
            "line": status_line(st, bool(ctrl.get("paused")), running, configured(out))}


def status_line(st: dict, paused: bool, running: bool, is_configured: bool) -> str:
    if not is_configured:
        return "Family sync: not set up"
    last = st.get("last_ok_at")
    when = _fmt_when(last) if last else "never"
    if paused:
        return f"Family sync: paused (last synced {when})"
    phase = st.get("phase")
    left = st.get("remaining_items") or 0
    if running and phase in ("sending", "preparing", "checking", "catalog"):
        b = st.get("remaining_bytes") or 0
        return f"Family sync: {left:,} items left ({_fmt_bytes(b)})" if left else "Family sync: working..."
    if running and phase == "waiting_window":
        return f"Family sync: waiting for the sync window (last synced {when})"
    if phase == "error":
        return f"Family sync: problem - {st.get('error') or 'see the Sync page'}"
    return f"Family sync: last synced {when}"


def _fmt_bytes(b: float) -> str:
    for unit, n in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if b >= n:
            return f"{b / n:.1f} {unit}"
    return f"{int(b)} bytes"


def _fmt_when(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    today = datetime.now().date()
    t = d.strftime("%I:%M %p").lstrip("0")
    if d.date() == today:
        return t
    return f"{d.strftime('%b')} {d.day}, {t}"


# ---- worker lock ------------------------------------------------------------------------------------------
class WorkerLock:
    """Exclusive lock file; released automatically if the process dies."""

    def __init__(self, out):
        self.path = _p(out, "sync.lock")
        self.fh = None

    def acquire(self) -> bool:
        self.fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.fh.close()
            self.fh = None
            return False

    def release(self) -> None:
        if self.fh:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self.fh.close()
            self.fh = None


def worker_alive(out) -> bool:
    lk = WorkerLock(out)
    if lk.acquire():
        lk.release()
        return False
    return True


def app_root() -> Path:
    from .config import install_dir

    return install_dir()


def worker_command(out) -> list[str]:
    if getattr(sys, "frozen", False):          # v0.5 installed app: PhotoSorter.exe sync --out ... (no window)
        return [str(Path(sys.executable).resolve()), "sync", "--out", str(out)]
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        exe = exe.parent / "pythonw.exe"          # no console window
    return [str(exe), "-m", "photosorter", "sync", "--out", str(out)]


def spawn_worker(out) -> bool:
    """Start the background worker (detached, low priority) unless one is already running."""
    if worker_alive(out):
        return False
    kw = dict(cwd=str(app_root()), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True)
    if sys.platform == "win32":
        kw["creationflags"] = (0x08000000 | 0x00000008 | 0x00000200 |   # NO_WINDOW, DETACHED, NEW_PROCESS_GROUP
                               background.BELOW_NORMAL_PRIORITY_CLASS)
    else:
        kw["start_new_session"] = True
    subprocess.Popen(worker_command(out), **kw)
    return True


def request_sync(out) -> dict:
    if not configured(out):
        raise ValueError("Set the calendar address and paste the pairing code first.")
    _update_control(out, now=time.time(), paused=False)
    started = spawn_worker(out)
    s = status(out)
    s["started_worker"] = started
    return s


def request_after_run(out) -> bool:
    cfg = load_config(out)
    if not (configured(out) and cfg["enabled"] and cfg["auto_after_run"]):
        return False
    _update_control(out, now=time.time())
    spawn_worker(out)
    return True


# ---- autostart (Windows Startup folder; no admin rights, no scheduled task) ---------------------------------
def startup_file() -> Path | None:
    if sys.platform != "win32":
        return None
    return (Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Microsoft" / "Windows" /
            "Start Menu" / "Programs" / "Startup" / "PhotoSorter-FamilySync.cmd")


def autostart_installed() -> bool:
    p = startup_file()
    return bool(p and p.exists())


def install_autostart(out) -> Path:
    """Start the worker at Windows sign-in: a small .cmd in the user's Startup folder (no admin rights).
    It starts with low priority and no window; the worker exits at once if one already runs."""
    p = startup_file()
    if p is None:
        raise RuntimeError("Autostart is for Windows")
    cmd = worker_command(out)
    line = 'start "" /low /b ' + " ".join(f'"{c}"' for c in cmd)
    p.write_text("@echo off\r\nrem Photo Sorter family sync (v0.4). Delete this file to stop it starting at sign-in.\r\n"
                 f'cd /d "{app_root()}"\r\n{line}\r\n', encoding="utf-8")
    return p


def refresh_autostart() -> bool:
    """v0.5: if sign-in autostart was set up by a source install, point it at the installed PhotoSorter.exe
    (same output folder). Returns True when the file was rewritten."""
    p = startup_file()
    if not (p and p.exists() and getattr(sys, "frozen", False)):
        return False
    txt = p.read_text(encoding="utf-8", errors="replace")
    if str(Path(sys.executable).resolve()) in txt:
        return False
    import re

    m = re.search(r'"--out" "([^"]+)"', txt)
    if not m:
        return False
    install_autostart(m.group(1))
    return True


def remove_autostart() -> bool:
    p = startup_file()
    if p and p.exists():
        p.unlink()
        return True
    return False


# ---- helpers ------------------------------------------------------------------------------------------------
def in_window(now: datetime, start: str, end: str) -> bool:
    """True when `now` is inside [start, end) local time; a window like 23:00-06:00 crosses midnight."""
    s = int(start[:2]) * 60 + int(start[3:])
    e = int(end[:2]) * 60 + int(end[3:])
    m = now.hour * 60 + now.minute
    if s == e:
        return True
    return s <= m < e if s < e else (m >= s or m < e)


class TokenBucket:
    """Average rate cap in bytes/second with a small burst."""

    def __init__(self, rate: float, burst: float | None = None, clock=time.monotonic, sleep=time.sleep):
        self.rate = max(1.0, float(rate))
        self.burst = burst if burst is not None else min(self.rate, 256 * 1024)
        self.tokens = self.burst
        self.clock, self.sleep = clock, sleep
        self.t = clock()

    def take(self, n: int) -> float:
        """Block until n bytes may be sent. Returns seconds slept."""
        slept = 0.0
        while True:
            now = self.clock()
            self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
            self.t = now
            if self.tokens >= n or n > self.burst and self.tokens >= self.burst:
                self.tokens -= n
                return slept
            wait = (min(n, self.burst) - self.tokens) / self.rate
            self.sleep(wait)
            slept += wait


def is_private_host(host: str) -> bool:
    """The address the name resolves to must be on the home network (private, link-local or loopback)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addrs = {i[4][0] for i in infos}
    if not addrs:
        return False
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            return False
    return True


class SyncError(Exception):
    pass


class PiClient:
    def __init__(self, base_url: str, token: str, verify_tls: bool = True, timeout: float = 60,
                 private_check=is_private_host):
        u = urlsplit(base_url)
        self.scheme, self.host, self.port = u.scheme, u.hostname, u.port
        self.token, self.verify_tls, self.timeout = token, verify_tls, timeout
        self.private_check = private_check
        self.conn = None

    def _connect(self):
        if not self.private_check(self.host):
            raise SyncError(f"{self.host} is not an address on your home network; refusing to send photos there.")
        if self.scheme == "https":
            ctx = ssl.create_default_context()
            if not self.verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self.conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=ctx)
        else:
            self.conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def request(self, method: str, path: str, body=None, headers: dict | None = None, length: int | None = None):
        h = {"Authorization": f"Bearer {self.token}", "User-Agent": f"PhotoSorter/{__version__}",
             "X-PhotoSync-Protocol": str(PROTOCOL), "Accept": "application/json"}
        h.update(headers or {})
        if isinstance(body, (bytes, bytearray)):
            h["Content-Length"] = str(len(body))
        elif body is not None:
            h["Content-Length"] = str(length)
        elif method == "POST":
            h["Content-Length"] = "0"
        for attempt in (1, 2):
            if self.conn is None:
                self._connect()
            try:
                self.conn.request(method, path, body=body, headers=h)
                res = self.conn.getresponse()
                data = res.read()
                break
            except (http.client.HTTPException, OSError) as e:
                self.close()
                if attempt == 2 or not isinstance(body, (bytes, bytearray, type(None))):
                    raise SyncError(f"Can't reach the calendar ({type(e).__name__}: {e})") from e
        try:
            js = json.loads(data or b"{}")
        except ValueError:
            js = {"error": data[:200].decode("utf-8", "replace")}
        if res.status == 401:
            raise SyncError("The calendar didn't accept the pairing code. Make a new one in the calendar admin "
                            "(Photos > Family photos) and paste it here.")
        return res.status, js

    def json(self, method: str, path: str, obj=None) -> dict:
        body = None if obj is None else json.dumps(obj).encode()
        st, js = self.request(method, path, body, {"Content-Type": "application/json"} if body else None)
        if st >= 400:
            raise SyncError(js.get("error") or f"HTTP {st}")
        return js


def test_connection(out) -> dict:
    cfg = load_config(out)
    tok = get_token(out)
    if not cfg["pi_url"] or not tok:
        return {"ok": False, "message": "Set the calendar address and paste the pairing code first."}
    cli = PiClient(cfg["pi_url"], tok, cfg["verify_tls"], timeout=15)
    try:
        st = cli.json("GET", "/api/photosync/status")
    except (SyncError, ValueError) as e:
        return {"ok": False, "message": str(e)}
    except ssl.SSLError as e:
        return {"ok": False, "message": f"Secure connection failed ({e.reason}). Use the calendar's https name."}
    finally:
        cli.close()
    if not st.get("writable"):
        return {"ok": False, "message": st.get("problem") or "The Pi can't write to its photo folder yet.",
                "pi": st}
    return {"ok": True, "message": f"Connected to the family calendar {st.get('version')}: "
                                   f"{_fmt_bytes(st.get('free_bytes') or 0)} free on the photo drive.", "pi": st}


# ---- hashing cache ------------------------------------------------------------------------------------------
class HashCache:
    def __init__(self, out):
        self.c = sqlite3.connect(str(_p(out, "sync-cache.sqlite")), timeout=30, check_same_thread=False)
        self.c.execute("CREATE TABLE IF NOT EXISTS hashes (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, "
                       "sha TEXT)")
        self.lock = threading.Lock()

    def sha(self, p: Path) -> tuple[str, int]:
        st = p.stat()
        with self.lock:
            r = self.c.execute("SELECT size, mtime_ns, sha FROM hashes WHERE path=?", (str(p),)).fetchone()
        if r and r[0] == st.st_size and r[1] == st.st_mtime_ns:
            return r[2], st.st_size
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for b in iter(lambda: f.read(CHUNK), b""):
                h.update(b)
        with self.lock:
            self.c.execute("INSERT OR REPLACE INTO hashes VALUES(?,?,?,?)", (str(p), st.st_size, st.st_mtime_ns,
                                                                            h.hexdigest()))
            self.c.commit()
        return h.hexdigest(), st.st_size

    def close(self):
        self.c.close()


# ---- what to send -------------------------------------------------------------------------------------------
def _face_crop(out: Path, db, face_id: int) -> Path | None:
    """A square-ish face picture for the family app's People grid (_index/sync-cache/face-<id>.jpg)."""
    from .imaging import open_image
    from . import thumbs

    d = _p(out, "sync-cache")
    d.mkdir(exist_ok=True)
    fp = d / f"face-{face_id}.jpg"
    if fp.exists():
        return fp
    fa = db.one("SELECT * FROM faces WHERE id=?", (face_id,))
    if not fa:
        return None
    r = db.one("SELECT id, src_path, dest_path, mtime, size FROM files WHERE id=?", (fa["file_id"],))
    if not r:
        return None
    try:
        im = open_image(thumbs.source_path(out, r), max_side=1600)
    except Exception:
        return None
    W, H = im.size
    x1, y1, x2, y2 = fa["x1"] * W, fa["y1"] * H, fa["x2"] * W, fa["y2"] * H
    pad = 0.35 * max(x2 - x1, y2 - y1)
    crop = im.crop((max(0, x1 - pad), max(0, y1 - pad), min(W, x2 + pad), min(H, y2 + pad)))
    crop.thumbnail((FACE_SIDE, FACE_SIDE))
    tmp = fp.with_name(fp.name + ".tmp")
    crop.save(tmp, "JPEG", quality=85)
    os.replace(tmp, fp)
    return fp


def build_plan(out, cfg: dict, db=None) -> dict:
    """Catalog records + the list of files to send (paths are resolved lazily)."""
    from .db import DB
    from . import thumbs

    out = Path(out)
    own = db is None
    db = db or DB(db_path(out))
    try:
        types = ("photo", "screenshot") if cfg.get("include_screenshots") else ("photo",)
        album_files = {r[0] for r in db.query("SELECT DISTINCT file_id FROM album_items")}
        rows = db.query(
            "SELECT f.id, f.filename, f.ext, f.taken_at, f.width, f.height, f.place, f.size, f.mtime, f.sha256, "
            "f.src_path, f.dest_path, f.type FROM files f LEFT JOIN file_review fr ON fr.file_id=f.id "
            "WHERE (f.is_keeper IS NULL OR f.is_keeper=1) AND (f.rep_id IS NULL OR f.rep_id=f.id) "
            "AND f.dest_path IS NOT NULL AND f.sha256 IS NOT NULL AND COALESCE(fr.state, '') <> 'remove' "
            "ORDER BY f.taken_at DESC, f.id DESC")
        rows = [r for r in rows if r["type"] in types or r["id"] in album_files]
        ids = {r["id"] for r in rows}
        people = [dict(p) for p in db.query(
            "SELECT id, name FROM people WHERE name IS NOT NULL AND name<>'' AND hidden=0 ORDER BY lower(name)")]
        pids = {p["id"] for p in people}
        person_of: dict[int, set] = {}
        for f in db.query("SELECT DISTINCT file_id, person_id FROM faces WHERE person_id IS NOT NULL"):
            if f["person_id"] in pids and f["file_id"] in ids:
                person_of.setdefault(f["file_id"], set()).add(f["person_id"])
        albums = [dict(a) for a in db.query("SELECT id, name FROM albums ORDER BY lower(name)")]
        album_of: dict[int, list] = {}
        for it in db.query("SELECT album_id, file_id FROM album_items ORDER BY added_at, file_id"):
            if it["file_id"] in ids:
                album_of.setdefault(it["file_id"], []).append(it["album_id"])
        recs, items = [], []
        ext_t = thumbs.EXT.lstrip(".")
        per_person: dict[int, int] = {}
        per_album: dict[int, list] = {}
        for r in rows:
            k = r["sha256"]
            oext = (r["ext"] or Path(r["filename"] or "").suffix or ".jpg").lower().lstrip(".")[:8] or "jpg"
            th, pv = f"{k}.t.{ext_t}", f"{k}.p.{ext_t}"
            o = f"{k}.o.{oext}" if cfg.get("include_full") else None
            rec = {"t": "photo", "id": r["id"], "k": k, "d": r["taken_at"], "w": r["width"], "h": r["height"],
                   "pl": r["place"], "fn": r["filename"], "ty": r["type"], "th": th, "pv": pv, "o": o,
                   "os": r["size"], "pp": sorted(person_of.get(r["id"], ())), "al": album_of.get(r["id"], [])}
            recs.append(rec)
            for pid in rec["pp"]:
                per_person[pid] = per_person.get(pid, 0) + 1
            for aid in rec["al"]:
                per_album.setdefault(aid, []).append(r["id"])
            row = {"id": r["id"], "src_path": r["src_path"], "dest_path": r["dest_path"], "mtime": r["mtime"],
                   "size": r["size"]}
            items.append({"name": th, "kind": "t", "row": row})
            items.append({"name": pv, "kind": "p", "row": row})
            if o:
                items.append({"name": o, "kind": "o", "row": row, "sha": k, "size": r["size"]})
        prec = []
        for p in people:
            if not per_person.get(p["id"]):
                continue
            cover = db.one("SELECT id FROM faces WHERE person_id=? ORDER BY locked DESC, low_quality ASC, "
                           "COALESCE(face_px,0) * COALESCE(score,0) DESC LIMIT 1", (p["id"],))
            prec.append({"t": "person", "id": p["id"], "n": p["name"], "count": per_person[p["id"]],
                         "face_id": cover[0] if cover else None})
        arec = [{"t": "album", "id": a["id"], "n": a["name"], "count": len(per_album.get(a["id"], [])),
                 "cover": (per_album.get(a["id"]) or [None])[0]} for a in albums if per_album.get(a["id"])]
        return {"photos": recs, "people": prec, "albums": arec, "items": items}
    finally:
        if own:
            db.close()


def catalog_bytes(plan: dict) -> bytes:
    lines = [json.dumps({"t": "meta", "v": PROTOCOL, "app": f"Photo Sorter {__version__}",
                         "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                         "photos": len(plan["photos"]), "people": len(plan["people"]),
                         "albums": len(plan["albums"])})]
    for rec in plan["people"] + plan["albums"] + plan["photos"]:
        lines.append(json.dumps(rec, separators=(",", ":")))
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:   # mtime=0: same catalog -> same bytes
        gz.write(raw)
    return buf.getvalue()


# ---- the sync run ------------------------------------------------------------------------------------------
class Stop(Exception):
    pass


class Syncer:
    def __init__(self, out, cfg: dict | None = None, token: str | None = None, client: PiClient | None = None,
                 manual: bool = False, run_mode: str | None = None, clock=time.time, sleep=time.sleep,
                 idle_fn=None):
        self.out = Path(out)
        self.cfg = cfg or load_config(out)
        self.token = token if token is not None else get_token(out)
        self.client = client or PiClient(self.cfg["pi_url"], self.token, self.cfg["verify_tls"])
        self.manual = manual
        self.clock, self.sleep = clock, sleep
        mode = run_mode or "background"
        kw = {"idle_fn": idle_fn} if idle_fn else {}
        self.gate = background.IdleGate("lowest" if mode == "lowest" else ("full" if mode == "full" else
                                                                            "background"), sleep=sleep, **kw)
        self.bucket = TokenBucket(self.cfg["max_mbps"] * 1024 * 1024, clock=time.monotonic, sleep=sleep)
        self.hc = HashCache(out)
        self.state = dict(read_state(out))
        self._last_save = 0.0

    # state
    def save_state(self, force: bool = False, **kw):
        self.state.update(kw)
        now = self.clock()
        if force or now - self._last_save > 1.0:
            self.state["heartbeat"] = now
            _write_json(_p(self.out, "sync-state.json"), self.state)
            self._last_save = now

    def checkpoint(self):
        """Between chunks: honour Pause and the time window (Sync now ignores the window)."""
        while True:
            ctrl = read_control(self.out)
            if ctrl.get("paused"):
                self.save_state(force=True, phase="paused")
                self.sleep(2)
                continue
            if self.cfg["window_enabled"] and not self.manual and not in_window(
                    datetime.fromtimestamp(self.clock()), self.cfg["window_start"], self.cfg["window_end"]):
                self.save_state(force=True, phase="waiting_window")
                self.sleep(30)
                continue
            return

    def _path_for(self, it: dict) -> Path | None:
        from . import thumbs

        if it["kind"] == "o":
            p = self.out / it["row"]["dest_path"]
            return p if p.is_file() else None
        if it["kind"] == "f":
            return it["path"]
        self.gate.wait()                      # making a preview is real CPU work: let the user go first
        try:
            return thumbs.ensure(self.out, it["row"], "thumb" if it["kind"] == "t" else "preview")
        except Exception:
            return None

    def _sha_for(self, it: dict, p: Path) -> tuple[str, int]:
        if it["kind"] == "o":
            st = p.stat()
            src = Path(it["row"]["src_path"])
            try:
                same = src.stat().st_size == st.st_size and abs(src.stat().st_mtime - st.st_mtime) < 2
            except OSError:
                same = False
            if same and st.st_size == it.get("size"):
                return it["sha"], st.st_size          # copy2 kept size + time: the indexed hash still holds
        return self.hc.sha(p)

    def upload(self, name: str, p: Path, sha: str, size: int, offset: int = 0) -> int:
        sent = 0
        with open(p, "rb") as f:                      # read-only
            while offset < size or size == 0:
                self.checkpoint()
                f.seek(offset)
                n = min(CHUNK, size - offset)
                data = f.read(n)
                if len(data) != n:
                    raise SyncError(f"{p.name} changed while sending")

                def gen(data=data):
                    for i in range(0, len(data), 16384):
                        piece = data[i:i + 16384]
                        self.bucket.take(len(piece))
                        yield piece
                q = f"/api/photosync/upload?name={quote(name)}&offset={offset}&total={size}&sha256={sha}"
                st, js = self.client.request("POST", q, gen() if data else b"",
                                             {"Content-Type": "application/octet-stream"}, length=len(data))
                if st == 409 and isinstance(js.get("offset"), int):
                    offset = js["offset"]            # the Pi has a different amount: carry on from there
                    if js.get("complete"):
                        return sent
                    continue
                if st >= 400:
                    raise SyncError(js.get("error") or f"upload failed (HTTP {st})")
                sent += len(data)
                offset = js.get("received", offset + len(data))
                self.state["bytes_sent"] = self.state.get("bytes_sent", 0) + len(data)
                self.state["remaining_bytes"] = max(0, (self.state.get("remaining_bytes") or 0) - len(data))
                self.save_state()
                if js.get("complete") or size == 0:
                    return sent
        return sent

    def run(self) -> dict:
        t0 = self.clock()
        self.save_state(force=True, phase="checking", error=None, started_at=t0, bytes_sent=0,
                        remaining_items=None, remaining_bytes=None)
        try:
            pi = self.client.json("GET", "/api/photosync/status")
            if not pi.get("writable"):
                raise SyncError(pi.get("problem") or "The Pi can't write to its photo folder.")
            self.save_state(force=True, phase="preparing")
            plan = build_plan(self.out, self.cfg)
            # one face picture per named person
            from .db import DB
            db = DB(db_path(self.out))
            try:
                for person in plan["people"]:
                    fp = _face_crop(self.out, db, person["face_id"]) if person.get("face_id") else None
                    if fp:
                        sha, size = self.hc.sha(fp)
                        person["c"] = f"{sha}.f.jpg"
                        plan["items"].insert(0, {"name": person["c"], "kind": "f", "path": fp})
            finally:
                db.close()
            cat = catalog_bytes(plan)
            csha = hashlib.sha256(cat).hexdigest()
            cname = f"{csha}.catalog.ndjson.gz"
            self.save_state(force=True, phase="catalog", photos=len(plan["photos"]), people=len(plan["people"]),
                            albums=len(plan["albums"]))
            tmp = _p(self.out, "sync-cache")
            tmp.mkdir(exist_ok=True)
            cpath = tmp / "catalog.ndjson.gz"
            cpath.write_bytes(cat)
            if self._missing([cname]).get(cname, 0) is not None:
                self.upload(cname, cpath, csha, len(cat), self._missing([cname]).get(cname) or 0)
            cres = self.client.json("POST", "/api/photosync/catalog", {"name": cname})
            # what's missing on the Pi
            order = {"f": 0, "t": 1, "p": 2, "o": 3}
            items = sorted(plan["items"], key=lambda it: order[it["kind"]])
            names = [it["name"] for it in items]
            missing: dict[str, int] = {}
            for i in range(0, len(names), 2000):
                missing.update({k: v for k, v in self._missing(names[i:i + 2000]).items() if v is not None})
            todo = [it for it in items if it["name"] in missing]
            est = sum(it.get("size") or EST_BYTES.get(it["kind"], 0) for it in todo)
            self.save_state(force=True, phase="sending", total_items=len(items), remaining_items=len(todo),
                            remaining_bytes=est, catalog=cname)
            sent_files = failed = 0
            for it in todo:
                self.checkpoint()
                p = self._path_for(it)
                if p is None:
                    failed += 1
                else:
                    sha, size = self._sha_for(it, p)
                    self.upload(it["name"], p, sha, size, missing.get(it["name"]) or 0)
                    sent_files += 1
                self.state["remaining_items"] = max(0, (self.state.get("remaining_items") or 1) - 1)
                self.save_state()
            res = self.client.json("POST", "/api/photosync/commit", {"name": cname})
            now = self.clock()
            self.save_state(force=True, phase="done", last_ok_at=now, last_run_at=now, error=None,
                            remaining_items=0, remaining_bytes=0, sent_files=sent_files, failed_files=failed,
                            removed_on_pi=res.get("removed", 0), seconds=round(now - t0, 1),
                            pi_photos=cres.get("photos"))
            return {"ok": True, "sent": sent_files, "failed": failed, "removed": res.get("removed", 0)}
        except Stop:
            self.save_state(force=True, phase="stopped")
            raise
        except (SyncError, OSError, ssl.SSLError) as e:
            self.save_state(force=True, phase="error", error=str(e)[:300], last_run_at=self.clock())
            return {"ok": False, "error": str(e)}
        finally:
            self.client.close()
            self.hc.close()

    def _missing(self, names: list[str]) -> dict:
        """{name: offset or 0} for files the Pi doesn't have complete; names it has map to None."""
        js = self.client.json("POST", "/api/photosync/check", {"names": names})
        have = set(js.get("have") or [])
        partial = js.get("partial") or {}
        return {n: (None if n in have else int(partial.get(n, 0))) for n in names}


# ---- worker process ---------------------------------------------------------------------------------------
def _run_mode() -> str:
    try:
        return json.loads((app_data_dir() / "gui.json").read_text(encoding="utf-8")).get("run_mode", "background")
    except Exception:
        return "background"


def daemon_main(out, once: bool = False, max_loops: int | None = None, sleep=time.sleep) -> int:
    out = Path(out)
    if not db_path(out).exists():
        print(f"No index in {out}", file=sys.stderr)
        return 2
    lock = WorkerLock(out)
    if not lock.acquire():
        return 0                                   # another worker already runs for this folder
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(app_data_dir() / "sync.log", encoding="utf-8")])
    mode = _run_mode()
    background.apply("lowest" if mode == "lowest" else "background")
    loops = 0
    try:
        handled_now = read_control(out).get("now_done", 0)
        while True:
            loops += 1
            if max_loops is not None and loops > max_loops:
                break
            cfg = load_config(out)
            ctrl = read_control(out)
            st = read_state(out)
            if not configured(out):
                if once:
                    break
                sleep(10)
                continue
            requested = (ctrl.get("now") or 0) > (ctrl.get("now_done") or handled_now or 0)
            due = cfg["enabled"] and time.time() - (st.get("last_run_at") or 0) >= cfg["interval_minutes"] * 60
            if ctrl.get("paused"):
                _write_json(_p(out, "sync-state.json"), {**st, "phase": "paused", "heartbeat": time.time()})
                sleep(3)
                continue
            if requested or due or once:
                if cfg["window_enabled"] and not requested and not in_window(
                        datetime.now(), cfg["window_start"], cfg["window_end"]):
                    _write_json(_p(out, "sync-state.json"), {**st, "phase": "waiting_window",
                                                              "heartbeat": time.time()})
                    sleep(30)
                    continue
                if requested:
                    _update_control(out, now_done=ctrl.get("now"))
                res = Syncer(out, cfg, manual=requested, run_mode=mode).run()
                log.info("sync finished: %s", {k: v for k, v in res.items() if k != "error"} if res.get("ok")
                         else res.get("error"))
                if once:
                    break
                sleep(5 if res.get("ok") else 300)
                continue
            sleep(5)
    finally:
        lock.release()
    return 0
