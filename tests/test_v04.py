"""v0.4: run in background, learning from Clean up choices, verify before deleting originals, family sync."""
import csv
import hashlib
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from photosorter import background, cleanup, prefs, sync, verify, albums
from photosorter.config import Settings, db_path
from photosorter.db import DB
from photosorter.pipeline import Pipeline, choose_keeper

from .conftest import exif_bytes, scene, tree_hashes


def run(src, out, **kw):
    s = Settings(sources=[str(src)], output=str(out), use_ai=False, use_faces=False, workers=2, **kw)
    return Pipeline(s).run()


@pytest.fixture
def folder_trash(monkeypatch):
    monkeypatch.setenv("PHOTOSORTER_TRASH", "folder")


# ---- run in background -----------------------------------------------------------------------------------
def test_thread_budget_and_worker_cap():
    assert background.thread_budget("full", 8) == 8
    assert background.thread_budget("background", 8) == 4
    assert background.thread_budget("lowest", 1) == 1
    s = Settings(workers=16, run_mode="background")
    assert background.worker_count(s) == max(1, background.cpu_count() // 2)
    assert background.worker_count(Settings(workers=2, run_mode="full")) == 2
    assert background.norm_mode("bogus") == "background"


def test_thread_env_only_capped_outside_full_speed():
    env = {}
    background.set_thread_env("full", env)
    assert "OMP_NUM_THREADS" not in env
    background.set_thread_env("background", env)
    assert env["OMP_NUM_THREADS"] == str(background.thread_budget("background"))
    user = {"OMP_NUM_THREADS": "3"}                       # the user's own setting wins
    background.set_thread_env("background", user)
    assert user["OMP_NUM_THREADS"] == "3"


def test_idle_gate_waits_while_user_is_active():
    slept = []
    idle = iter([2.0, 5.0, 40.0])                        # seconds since input: active, active, idle
    g = background.IdleGate("background", resume_s=30, max_wait=20, idle_fn=lambda: next(idle),
                            sleep=slept.append)
    assert g.wait() == 2.0 and slept == [1.0, 1.0]
    busy = background.IdleGate("background", resume_s=30, max_wait=3, idle_fn=lambda: 0.0, sleep=lambda s: None)
    assert busy.wait() == 3.0                             # throttled, never stuck
    full = background.IdleGate("full", idle_fn=lambda: 0.0, sleep=lambda s: pytest.fail("full speed waits"))
    assert full.wait() == 0.0
    unknown = background.IdleGate("background", idle_fn=lambda: None, sleep=lambda s: pytest.fail("no idle info"))
    assert unknown.wait() == 0.0


def test_apply_lowers_priority_and_caps_ort(monkeypatch):
    info = background.apply("background")
    assert info["mode"] == "background" and background.ort_threads() == background.thread_budget("background")
    background._state["mode"] = "full"
    assert background.ort_threads() == 0


# ---- learning from Clean up choices -----------------------------------------------------------------------
def member(i, w, h, size, path, exif=True, camera=True, ext=".jpg", **kw):
    d = {"id": i, "src_path": path, "filename": Path(path).name, "width": w, "height": h, "size": size,
         "date_source": "exif" if exif else "mtime", "camera_make": "Apple" if camera else None,
         "camera_model": "iPhone" if camera else None, "lat": None, "ext": ext, "taken_at": "2023-05-01T10:00:00",
         "_sharpness": None, "review": None, "organized": 0, "dest_path": None}
    d.update(kw)
    return d


class FakeDB:
    """Just enough of DB for prefs (an in-memory sqlite with the real table)."""

    def __init__(self, tmp):
        self.db = DB(Path(tmp) / "t.sqlite")

    def __getattr__(self, k):
        return getattr(self.db, k)


def edited_pair(i):
    """The user's habit: keep the camera original over a bigger, sharper-looking edited export."""
    orig = member(2 * i, 3000, 2000, 3_000_000, f"D:/Phone/DCIM/IMG_{i:04}.jpg")
    export = member(2 * i + 1, 4000, 3000, 6_000_000, f"D:/Pictures/Exports/IMG_{i:04}-edited.jpg")
    return orig, export


def test_without_decisions_the_old_quality_order_applies(tmp_path):
    db = FakeDB(tmp_path)
    m = prefs.load_model(db)
    assert not m.active and m.groups == 0
    orig, export = edited_pair(1)
    assert choose_keeper([orig, export], None, m)[0] == export["id"]       # higher resolution wins


def test_learning_shifts_the_suggestion_and_explains_why(tmp_path):
    db = FakeDB(tmp_path)
    orig, export = edited_pair(100)
    # two choices: below MIN_GROUPS, nothing changes yet
    for i in range(2):
        o, e = edited_pair(i)
        prefs.record_decision(db, 1000 + i, i, [o, e], [o["id"]])
    assert not prefs.load_model(db).active
    assert choose_keeper([orig, export], None, prefs.load_model(db))[0] == export["id"]
    # three: active, but a few decisions must not swing a normal group (bigger original vs small copy)
    o, e = edited_pair(2)
    prefs.record_decision(db, 1002, 2, [o, e], [o["id"]])
    m = prefs.load_model(db)
    assert m.active
    big = member(900, 4000, 3000, 4_000_000, "D:/Phone/DCIM/IMG_9.jpg")
    small = member(901, 1600, 1200, 400_000, "D:/Phone/DCIM/IMG_9 (1).jpg")
    assert choose_keeper([small, big], None, m)[0] == big["id"]
    # consistent choices over many groups: the edited export no longer wins
    for i in range(3, 12):
        o, e = edited_pair(i)
        prefs.record_decision(db, 1000 + i, i, [o, e], [o["id"]])
    m = prefs.load_model(db)
    assert m.groups == 12
    best, feats, score = m.rank([orig, export])
    assert best == orig["id"] and score[orig["id"]] > 0.6
    assert choose_keeper([orig, export], None, m)[0] == orig["id"]
    why = m.why(feats[orig["id"]], feats[export["id"]])
    assert why == "the unedited original"
    assert "the unedited original" in m.top_lessons()
    # still sensible for an unrelated normal group
    assert choose_keeper([small, big], None, m)[0] == big["id"]
    # reset forgets everything
    assert prefs.reset(db) == 12
    assert not prefs.load_model(db).active


def test_features_cover_the_listed_signals():
    f = prefs.features(member(1, 4032, 3024, 2_000_000, r"C:\Users\a\WhatsApp Images\IMG-20230101-WA0003 (1).jpeg",
                              exif=False, camera=False, ext=".jpeg", software="Snapseed", type="photo"))
    v = dict(zip(prefs.NAMES, f["v"]))
    assert v["whatsapp"] == 1 and v["copy_name"] == 1 and v["edited"] == 1 and v["jpeg"] == 1
    assert v["exif_date"] == 0 and v["camera"] == 0
    s = dict(zip(prefs.NAMES, prefs.features(member(2, 1170, 2532, 1, r"C:\Pics\Screenshots\Screenshot_1.png",
                                                    ext=".png"))["v"]))
    assert s["screenshot"] == 1 and s["png"] == 1
    k = dict(zip(prefs.NAMES, prefs.features(member(3, 0, 0, 1, "a.heic", ext=".heic", taken_at=None))["k"]))
    assert k["log_pixels"] == 0 and k["date_later"] == 0 and k["sharpness"] == 0


def test_cleanup_records_real_choices_only(sample_src, tmp_path, folder_trash):
    out = tmp_path / "out"
    run(sample_src, out)
    db = DB(db_path(out))
    gid = db.one("SELECT dup_group FROM files WHERE filename='IMG_0001_small.jpg'")[0]
    d = cleanup.group_detail(db, gid, out)
    keep = {m["id"] for m in d["members"] if m["proposed"] == "keep"}
    n_members = len(d["members"])
    r = cleanup.apply_decision(db, out, gid, keep)
    assert db.one("SELECT COUNT(*) FROM keep_decisions")[0] == n_members - len(keep)
    assert db.one("SELECT COUNT(DISTINCT action_id) FROM keep_decisions")[0] == 1
    cleanup.undo_last(db, out)                                  # an undone choice teaches nothing
    assert db.one("SELECT COUNT(*) FROM keep_decisions")[0] == 0
    cleanup.apply_decision(db, out, gid, [m["id"] for m in d["members"]])   # Keep all
    assert db.one("SELECT COUNT(*) FROM keep_decisions")[0] == 0
    cleanup.undo_last(db, out)
    cleanup.delete_group(db, out, gid)                           # Delete all
    assert db.one("SELECT COUNT(*) FROM keep_decisions")[0] == 0
    cleanup.undo_last(db, out)
    cleanup.approve_all(db, out)                                 # Approve all: accepted defaults, not a choice
    assert db.one("SELECT COUNT(*) FROM keep_decisions")[0] == 0


def test_learned_preference_reranks_a_pending_group(sample_src, tmp_path, folder_trash):
    """Someone who always keeps the copy over the identical original (say, the one in their backup folder named
    "copy") gets that suggested in groups still waiting for review, with a reason."""
    out = tmp_path / "out"
    run(sample_src, out)
    db = DB(db_path(out))
    gid = db.one("SELECT dup_group FROM files WHERE filename='IMG_0001_small.jpg'")[0]
    copy_id = db.one("SELECT id FROM files WHERE filename='IMG_0001 copy.jpg'")[0]
    before = cleanup.group_detail(db, gid, out)
    assert before["recommended_keeper_id"] != copy_id
    for i in range(10):
        a = member(10 * i, 4000, 3000, 3_000_000, f"D:/Phone/DCIM/IMG_{i}.jpg")
        b = member(10 * i + 1, 4000, 3000, 3_000_000, f"D:/Backup/IMG_{i} copy.jpg")
        prefs.record_decision(db, 5000 + i, 9000 + i, [a, b], [b["id"]])
    db.commit()
    after = cleanup.group_detail(db, gid, out)
    assert after["recommended_keeper_id"] == copy_id
    assert after["reasons"][0].startswith("Learned from your choices: you usually keep")
    assert "copy" in after["reasons"][0]
    assert [m["id"] for m in after["members"] if m["proposed"] == "keep"] == [copy_id]
    # the web page can show and reset it
    from fastapi.testclient import TestClient
    from photosorter.web.app import create_app
    c = TestClient(create_app(out))
    st = c.get("/api/cleanup/learning").json()
    assert st["active"] and st["decisions"] == 10 and st["lessons"]
    assert c.post("/api/cleanup/learning/reset", json={"confirm": True}).status_code == 403   # needs header
    assert c.post("/api/cleanup/learning/reset", json={"confirm": True}, headers={"X-PhotoSorter": "1"}).json()["forgot"] == 10
    assert cleanup.group_detail(db, gid, out)["recommended_keeper_id"] == before["recommended_keeper_id"]


# ---- verify before deleting originals ----------------------------------------------------------------------
def test_verify_buckets_rescue_and_originals_untouched(sample_src, tmp_path, folder_trash):
    src = sample_src
    scene(2).resize((800, 600)).save(src / "downloads/IMG_0002_small.jpg", quality=70)    # near dup of IMG_0002
    out = tmp_path / "out"
    run(src, out)
    db = DB(db_path(out))
    # an album (hard links) must not confuse anything
    a = albums.create_album(db, out, "Best")
    albums.add_files(db, out, a["id"], [db.one("SELECT id FROM files WHERE filename='IMG_0002.jpg'")[0]])
    # group 1: the small copy removed on purpose in Clean up
    g1 = db.one("SELECT dup_group FROM files WHERE filename='IMG_0001_small.jpg'")[0]
    d = cleanup.group_detail(db, g1, out)
    cleanup.apply_decision(db, out, g1, {m["id"] for m in d["members"] if m["proposed"] == "keep"})
    # group 2: the small copy's library file deleted by hand (no decision) -> only a look-alike remains
    row = db.one("SELECT dest_path, dup_group FROM files WHERE filename='IMG_0002_small.jpg'")
    assert row["dup_group"]
    (out / row["dest_path"]).unlink()
    (src / "downloads/new_after_run.jpg").write_bytes(b"\xff\xd8 new")    # not sorted yet
    (src / "phone/DCIM/clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42 video")   # videos aren't sorted
    before = tree_hashes(src)
    stat_before = {p: os.stat(p).st_mtime_ns for p in src.rglob("*") if p.is_file()}

    res = verify.run_verify(out)["summary"]
    rows = {Path(r["path"]).name: r for r in verify.results(out, limit=1000)}
    b = lambda n: rows[n]["bucket"]
    assert b("IMG_0001.jpg") == "safe_copy" and b("IMG_0001 copy.jpg") == "safe_copy"
    assert b("IMG_0002.jpg") == "safe_copy" and b("Screenshot_20200105-101010.png") == "safe_copy"
    assert b("IMG_0001_small.jpg") == "safe_deduped"
    assert b("IMG_0002_small.jpg") == "near_only"
    assert b("notes.txt") == "missing" and "not a photo type" in rows["notes.txt"]["reason"]
    assert b("corrupt.jpg") == "safe_copy"                 # unreadable photos are still copied (Other)
    assert b("clip.mp4") == "missing"
    assert b("new_after_run.jpg") == "missing" and "not in the index" in rows["new_after_run.jpg"]["reason"]
    assert res["attention"] == 4 and not res["all_safe"] and "need attention" in res["banner"]
    assert res["buckets"]["missing"]["bytes"] > 0
    assert "backup" in res["advice"] and "never deletes" in res["advice"]
    # CSV of every file
    with open(verify.csv_path(out), encoding="utf-8-sig") as f:
        lines = list(csv.DictReader(f))
    assert len(lines) == len(rows) and {"original", "result", "safe", "reason"} <= set(lines[0])
    # rescue copies buckets 3-4, keeping relative paths
    r = verify.rescue(out)
    assert r["copied"] == 4 and not r["errors"]
    assert (out / "Rescued" / "src" / "downloads" / "notes.txt").read_text() == "ignore me"
    assert (out / "Rescued" / "src" / "downloads" / "IMG_0002_small.jpg").exists()
    assert (out / "Rescued" / "src" / "phone" / "DCIM" / "clip.mp4").exists()
    assert verify.rescue(out)["skipped"] == 4                 # running it again copies nothing twice
    # originals: byte-for-byte and timestamp-for-timestamp unchanged, folder still there
    assert tree_hashes(src) == before
    assert {p: os.stat(p).st_mtime_ns for p in src.rglob("*") if p.is_file()} == stat_before
    # with the rescued copies in the library, everything is safe now
    res2 = verify.run_verify(out)["summary"]
    assert res2["all_safe"] and res2["banner"].startswith("100% safe")
    assert tree_hashes(src) == before


def test_verify_hash_cache_and_web_api(sample_src, tmp_path, monkeypatch):
    out = tmp_path / "out"
    run(sample_src, out)
    verify.run_verify(out)
    calls = []
    real = verify.sha256_file
    monkeypatch.setattr(verify, "sha256_file", lambda p, *a, **k: calls.append(p) or real(p, *a, **k))
    verify.run_verify(out)
    assert calls == []                                       # nothing re-read when unchanged
    (sample_src / "downloads/notes.txt").write_text("changed!")
    verify.run_verify(out)
    assert [Path(p).name for p in calls] == ["notes.txt"]
    from fastapi.testclient import TestClient
    from photosorter.web.app import create_app
    c = TestClient(create_app(out))
    assert "Verify before deleting originals" in c.get("/verify").text
    s = c.get("/api/verify/status").json()
    assert s["summary"]["finished_at"] and not s["running"]
    assert c.post("/api/verify/rescue", json={"confirm": True}).status_code == 403
    assert c.get("/api/verify/csv").headers["content-type"].startswith("text/csv")
    assert c.get("/api/verify/results?bucket=bogus").status_code == 400
    assert c.post("/api/verify/start", headers={"X-PhotoSorter": "1"}).json()["started"]
    for _ in range(100):
        if not c.get("/api/verify/status").json()["running"]:
            break
        time.sleep(0.1)
    assert c.get("/api/verify/status").json()["progress"]["phase"] == "done"


# ---- family sync ---------------------------------------------------------------------------------------------
def test_token_bucket_caps_the_rate():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s
    b = sync.TokenBucket(1000, burst=500, clock=lambda: t[0], sleep=sleep)
    for _ in range(10):
        b.take(500)
    assert 4.4 <= t[0] <= 4.6                              # 5000 bytes at 1000 B/s, first 500 free


def test_window_crossing_midnight():
    from datetime import datetime as dt
    assert sync.in_window(dt(2026, 1, 1, 23, 30), "23:00", "06:00")
    assert sync.in_window(dt(2026, 1, 1, 5, 59), "23:00", "06:00")
    assert not sync.in_window(dt(2026, 1, 1, 12, 0), "23:00", "06:00")
    assert sync.in_window(dt(2026, 1, 1, 12, 0), "09:00", "17:00")


def test_config_validation_and_token_storage(tmp_path):
    out = tmp_path / "out"
    (out / "_index").mkdir(parents=True)
    st = sync.update_config(out, {"pi_url": "https://cal.example.org/admin/", "max_mbps": 1.5,
                                  "token": "A" * 43, "window_enabled": True})
    assert st["config"]["pi_url"] == "https://cal.example.org" and st["paired"] and st["configured"]
    assert "A" * 43 not in (out / "_index" / "sync.json").read_text()          # token kept elsewhere
    assert "token" not in str(st)
    for bad in ({"pi_url": "ftp://x"}, {"max_mbps": 0}, {"window_start": "25:00"}, {"token": "short"},
                {"pi_url": "https://user:pw@x.org"}):
        with pytest.raises(ValueError):
            sync.update_config(out, bad)
    assert sync.status_line({}, False, False, False) == "Family sync: not set up"
    assert "paused" in sync.status_line({"last_ok_at": time.time()}, True, True, True)


def test_refuses_public_addresses():
    assert sync.is_private_host("127.0.0.1") and sync.is_private_host("192.168.1.50")
    assert not sync.is_private_host("8.8.8.8")
    c = sync.PiClient("https://8.8.8.8", "t" * 40)
    with pytest.raises(sync.SyncError, match="home network"):
        c.request("GET", "/api/photosync/status")


def _photos_src():
    for p in (os.environ.get("FAMILY_PHOTOS_SRC"), Path(__file__).resolve().parents[2] / "family-photos"):
        if p and (Path(p) / "family_photos" / "library.py").exists():
            return Path(p)
    return None


@pytest.fixture
def pi(tmp_path, monkeypatch):
    """A real Family Photos receiver (FAMILY_PHOTOS_SRC) on 127.0.0.1 (moved out of family-calendar in 0.5.1)."""
    src = _photos_src()
    if not src:
        pytest.skip("family-photos source not found (set FAMILY_PHOTOS_SRC)")
    sys.path.insert(0, str(src))
    from family_photos.config import load_config
    from family_photos.server import App, Server, make_handler
    from family_photos.auth import hash_password
    base = tmp_path / "pi"
    (base / "ssd").mkdir(parents=True)
    env = {"PUBLIC_BASE_URL": "", "DATA_DIR": str(base / "data"), "LOCAL_HTTP_BIND": "off",
           "TLS_CERT_FILE": "", "TLS_KEY_FILE": "",
           "ADMIN_PASSWORD_HASH": hash_password("x", iterations=1000), "ADMIN_PASSWORD": "",
           "LIBRARY_DIR": str(base / "ssd" / "family-photos")}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    app = App(load_config(base / "none.env"))
    app.family = app.lib                                       # the receiver, as the tests below call it
    httpd = Server(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_old_calendar_receiver_says_moved(tmp_path):
    """family calendar 0.1.18+ answers /api/photosync/* with 410 {"moved": true}: tell the user the new address."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"error": "moved", "moved": true, "app": "family-calendar"}'
            self.send_response(410)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        out = tmp_path / "out"
        out.mkdir()
        sync.update_config(out, {"pi_url": f"http://127.0.0.1:{httpd.server_address[1]}", "token": "t" * 43})
        res = sync.test_connection(out)
        assert not res["ok"] and "https://127.0.0.1:8443" in res["message"] and "Family Photos" in res["message"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_sync_end_to_end_resume_and_deletions(sample_src, tmp_path, pi, monkeypatch, folder_trash):
    app, url = pi
    out = tmp_path / "out"
    run(sample_src, out)
    db = DB(db_path(out))
    keep_id = db.one("SELECT id FROM files WHERE filename='IMG_0002.jpg'")[0]
    al = albums.create_album(db, out, "Christmas 2021")
    albums.add_files(db, out, al["id"], [keep_id])
    tok = app.family.new_token()
    sync.update_config(out, {"pi_url": url, "token": tok, "max_mbps": 50, "include_full": True})
    assert sync.test_connection(out)["ok"]
    monkeypatch.setattr(sync, "CHUNK", 8192)                 # many chunks per file
    before = tree_hashes(sample_src)
    lib_before = tree_hashes(out / "Photos")

    # a half-finished upload from an earlier, interrupted run
    plan = sync.build_plan(out, sync.load_config(out))
    first_o = next(it for it in plan["items"] if it["kind"] == "o")
    data = (out / first_o["row"]["dest_path"]).read_bytes()
    st, body = app.family.receive(first_o["name"], 0, len(data), first_o["sha"], 8192,
                                  __import__("io").BytesIO(data[:8192]))
    assert st == 200 and not body["complete"]
    sent_offsets = []
    real_upload = sync.Syncer.upload

    def spy(self, name, p, sha, size, offset=0):
        sent_offsets.append((name, offset))
        return real_upload(self, name, p, sha, size, offset)
    monkeypatch.setattr(sync.Syncer, "upload", spy)

    res = sync.Syncer(out, run_mode="full").run()
    assert res["ok"], res
    assert (first_o["name"], 8192) in sent_offsets             # resumed, not restarted
    meta = app.family.meta()
    n_photos = len(plan["photos"])
    assert meta["total"] == n_photos and n_photos >= 4
    assert [a["name"] for a in meta["albums"]] == ["Christmas 2021"]
    items = app.family.browse(album=al["id"])["items"]
    assert len(items) == 1 and items[0]["o"]
    got = app.family.media_path(items[0]["o"].rsplit("/", 1)[1]).read_bytes()
    assert got == (out / db.one("SELECT dest_path FROM files WHERE id=?", (keep_id,))[0]).read_bytes()
    state = sync.read_state(out)
    assert state["phase"] == "done" and state["remaining_items"] == 0
    assert "last synced" in sync.status(out)["line"]

    # nothing changed: second run sends nothing
    sent_offsets.clear()
    assert sync.Syncer(out, run_mode="full").run()["ok"]
    assert [n for n, _ in sent_offsets if ".catalog." not in n] == []

    # a photo removed in Clean up disappears from the Pi; the TV album list follows the catalog
    gid = db.one("SELECT dup_group FROM files WHERE filename='IMG_0001_small.jpg'")[0]
    small = db.one("SELECT sha256 FROM files WHERE filename='IMG_0001_small.jpg'")[0]
    d = cleanup.group_detail(db, gid, out)
    cleanup.apply_decision(db, out, gid, {m["id"] for m in d["members"] if m["proposed"] == "keep"})
    small_on_pi = list((app.family.lib / "media").rglob(f"{small}.*"))
    assert sync.Syncer(out, run_mode="full").run()["ok"]
    assert app.family.meta()["total"] == n_photos - (1 if small_on_pi else 0)
    assert list((app.family.lib / "media").rglob(f"{small}.*")) == []
    assert sync.read_state(out)["removed_on_pi"] >= (3 if small_on_pi else 0)
    # the PC side never changed: originals and the sorted library are only read
    assert tree_hashes(sample_src) == before
    assert {k: v for k, v in tree_hashes(out / "Photos").items()} == lib_before


def test_sync_pause_and_wrong_token(sample_src, tmp_path, pi):
    app, url = pi
    out = tmp_path / "out"
    run(sample_src, out)
    app.family.new_token()
    sync.update_config(out, {"pi_url": url, "token": "x" * 40})
    res = sync.Syncer(out, run_mode="full").run()
    assert not res["ok"] and "pairing code" in res["error"]
    assert sync.read_state(out)["phase"] == "error"
    assert "problem" in sync.status(out)["line"]
    # pause is honoured between chunks
    sync.update_config(out, {"token": app.family.new_token()})
    sync.set_paused(out, True)
    naps = []

    def nap(s):
        naps.append(s)
        if len(naps) == 3:
            sync.set_paused(out, False)
    s = sync.Syncer(out, run_mode="full", sleep=nap)
    assert s.run()["ok"]
    assert len(naps) >= 3


def test_worker_lock_and_daemon_once(sample_src, tmp_path, pi):
    app, url = pi
    out = tmp_path / "out"
    run(sample_src, out)
    assert not sync.worker_alive(out)
    lk = sync.WorkerLock(out)
    assert lk.acquire()
    assert sync.worker_alive(out)
    assert sync.daemon_main(out, once=True) == 0              # another worker holds the lock: exits at once
    lk.release()
    sync.update_config(out, {"pi_url": url, "token": app.family.new_token()})
    assert sync.daemon_main(out, once=True, sleep=lambda s: None) == 0
    assert sync.read_state(out)["phase"] == "done"
    assert app.family.meta()["total"] > 0


def test_sync_pages_and_api(sample_src, tmp_path):
    out = tmp_path / "out"
    run(sample_src, out)
    from fastapi.testclient import TestClient
    from photosorter.web.app import create_app
    c = TestClient(create_app(out))
    assert "Family sync to the calendar Pi" in c.get("/sync").text
    assert c.get("/api/sync/status").json()["line"] == "Family sync: not set up"
    assert c.post("/api/sync/config", json={"pi_url": "http://192.168.1.50:8080"}).status_code == 403
    r = c.post("/api/sync/config", json={"pi_url": "http://192.168.1.50:8080"}, headers={"X-PhotoSorter": "1"})
    assert r.json()["config"]["pi_url"] == "http://192.168.1.50:8080"
    assert c.post("/api/sync/config", json={"max_mbps": -1}, headers={"X-PhotoSorter": "1"}).status_code == 400
    assert c.post("/api/sync/now", headers={"X-PhotoSorter": "1"}).status_code == 409    # no pairing code yet
    for page in ("/", "/people", "/albums", "/cleanup", "/verify", "/sync"):
        assert 'href="/sync"' in c.get(page).text
