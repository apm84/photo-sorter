"""v0.6: Delete on selections, and videos (scan/copy, dates and places, frame sampling, previews, playable copies,
duplicates, web player API, verify, and sync to Family Photos). Video tests need ffmpeg/ffprobe (skipped without)."""
import io
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from photosorter import cleanup, sync, verify, video
from photosorter.config import Settings, db_path
from photosorter.db import DB
from photosorter.pipeline import Pipeline, pick_video_faces

from .conftest import exif_bytes, scene, tree_hashes
from .test_v04 import pi  # noqa: F401  (the real Family Photos receiver fixture)
from .test_v04 import sync_album_of_everything

H = {"X-PhotoSorter": "1"}
needs_ffmpeg = pytest.mark.skipif(not video.available(), reason="ffmpeg/ffprobe not found")


@pytest.fixture
def folder_trash(monkeypatch):
    monkeypatch.setenv("PHOTOSORTER_TRASH", "folder")


def run(src, out, **kw):
    s = Settings(sources=[str(src)], output=str(out), use_ai=False, use_faces=False, workers=2, **kw)
    return Pipeline(s).run()


def ff(*args):
    subprocess.run([video.tool("ffmpeg"), "-v", "error", "-y", *args], check=True, timeout=120)


# ---- units (no ffmpeg needed) ------------------------------------------------------------------------------
def test_sample_times_and_poster():
    assert video.sample_times(0) == [0.0]
    assert video.sample_times(3, 8) == [0.75, 2.25]                 # short clip: about one per 2 s
    t = video.sample_times(100, 8)
    assert len(t) == 8 and t[0] > 0 and t[-1] < 100 and t == sorted(t)
    assert video.poster_time(5) == 0.5 and video.poster_time(600) == 3.0


def test_video_metadata_parsing():
    assert video.parse_iso6709("+39.7392-104.9903+1609.000/") == (39.7392, -104.9903)
    assert video.parse_iso6709("+00.0000+000.0000/") is None
    assert video.parse_iso6709("garbage") is None
    assert str(video.parse_video_time("2023-05-01T13:22:11-0500")) == "2023-05-01 13:22:11"   # wall time kept
    assert video.parse_video_time("1904-01-01T00:00:00Z") is None
    assert video.parse_video_time("1970-01-01T00:00:00.000000Z") is None
    assert video.parse_video_time("2999-01-01T00:00:00Z") is None
    assert video.browser_playable({"vcodec": "h264", "acodec": "aac", "format": "mov,mp4,m4a", "pix_fmt": "yuv420p",
                                   "hdr": False})
    assert not video.browser_playable({"vcodec": "hevc", "acodec": "aac", "format": "mov,mp4,m4a",
                                       "pix_fmt": "yuv420p", "hdr": False})


def test_one_face_per_person_per_video():
    import numpy as np
    a, b = np.zeros(128, np.float32), np.zeros(128, np.float32)
    a[0], b[1] = 1, 1
    faces = [{"emb": a, "t": 1.0, "q": 1.0}, {"emb": a * 1.0, "t": 2.0, "q": 2.0}, {"emb": b, "t": 3.0, "q": 0.5}]
    picked = pick_video_faces(faces)
    assert [f["t"] for f in picked] == [2.0, 3.0]   # the clearest face of each person


def test_tools_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("PHOTOSORTER_FFMPEG_DIR", "none")
    video.reset_tools()
    try:
        assert not video.available()
    finally:
        monkeypatch.undo()               # back to the CI/bundled setting, then look again
        video.reset_tools()


# ---- a small library with videos ---------------------------------------------------------------------------
@pytest.fixture
def vsrc(tmp_path):
    if not video.available():
        pytest.skip("ffmpeg/ffprobe not found")
    src = tmp_path / "vsrc"
    (src / "phone").mkdir(parents=True)
    (src / "backup").mkdir()
    scene(11).save(src / "phone/IMG_0010.jpg", quality=90, exif=exif_bytes("2022:06:01 10:00:00", "Apple", "iPhone"))
    # a phone video with a date and a place (QuickTime creation_time + ©xyz location)
    ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=6", "-f", "lavfi", "-i",
       "sine=frequency=440:duration=6", "-c:v", _h264(), "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-shortest", "-metadata", "creation_time=2022-06-01T12:00:00Z",
       "-metadata", "location=+39.7392-104.9903/", str(src / "phone/VID_0001.mp4"))
    shutil.copy2(src / "phone/VID_0001.mp4", src / "backup/VID_0001 copy.mp4")              # exact duplicate
    ff("-i", str(src / "phone/VID_0001.mp4"), "-vf", "scale=320:180", "-c:v", "mpeg4", "-q:v", "6", "-an",
       str(src / "backup/VID_0001_small.mp4"))                                               # look-alike copy
    # an old camera clip browsers can't play (needs the H.264 copy), no date inside: file time is used
    ff("-f", "lavfi", "-i", "mandelbrot=size=320x240:rate=15", "-t", "3", "-c:v", "mpeg4", str(src / "backup/MVI_0002.avi"))
    os.utime(src / "backup/MVI_0002.avi", (1262340000, 1262340000))                          # 2010-01-01
    # a Live Photo: picture + 2 s clip with the same name
    scene(12).save(src / "phone/IMG_0100.jpg", quality=90, exif=exif_bytes("2022:07:04 12:00:00", "Apple", "iPhone"))
    ff("-f", "lavfi", "-i", "life=size=320x240:rate=15", "-t", "2", "-c:v", "mpeg4", "-tag:v", "mp4v",
       str(src / "phone/IMG_0100.mov"))
    return src


def _h264():
    """An H.264 encoder for the test videos (libx264 on Linux; the LGPL Windows build has OpenH264 / Media
    Foundation), else MPEG-4 Part 2 (then the 'playable' checks are skipped)."""
    if _enc("libx264"):
        return "libx264"
    return video.h264_encoder() or "mpeg4"


def _enc(name):
    try:
        r = subprocess.run([video.tool("ffmpeg"), "-hide_banner", "-encoders"], capture_output=True, text=True,
                           timeout=30)
        return f" {name} " in r.stdout
    except Exception:
        return False


def _row(db, name):
    return db.one("SELECT * FROM files WHERE filename=?", (name,))


@needs_ffmpeg
def test_videos_are_copied_with_dates_places_previews_and_duplicates(vsrc, tmp_path):
    before = tree_hashes(vsrc)
    out = tmp_path / "out"
    summary = run(vsrc, out)
    assert "videos" in summary
    db = DB(db_path(out))
    v = _row(db, "VID_0001.mp4")
    assert v["media"] == "video" and v["type"] == "video" and abs(v["duration"] - 6) < 0.2
    assert v["taken_at"].startswith("2022-06-01") and v["date_source"] == "metadata"
    assert round(v["lat"], 3) == 39.739 and round(v["lon"], 3) == -104.990
    assert v["playable"] == (1 if _h264() != "mpeg4" else 0) and v["dest_path"].replace("\\", "/").startswith("Photos/2022/2022-06")
    assert (out / v["dest_path"]).read_bytes() == (vsrc / "phone/VID_0001.mp4").read_bytes()
    old = _row(db, "MVI_0002.avi")
    assert old["date_source"] == "mtime" and old["taken_at"].startswith("2010-01-0") and old["playable"] == 0
    assert _row(db, "IMG_0100.mov")["live"] == 1 and not _row(db, "VID_0001.mp4")["live"]
    # exact copy is not copied again; the look-alike lands in the same duplicate group
    small = _row(db, "VID_0001_small.mp4")
    assert small["dup_group"] and small["dup_group"] == v["dup_group"]
    gid = v["dup_group"]
    assert cleanup.group_detail(db, gid, out)["recommended_keeper_id"] == v["id"]
    assert any(m["media"] == "video" and m["duration"] for m in cleanup.group_detail(db, gid, out)["members"])
    # previews: sprite for every video, H.264 copy only for the one browsers can't play
    assert video.sprite_path(out, v["id"]).exists()
    vv = db.one("SELECT * FROM videos WHERE file_id=?", (v["id"],))
    assert vv["sprite_n"] >= 5 and vv["tile_w"] == 160
    if _h264() != "mpeg4":
        assert video.proxy_state(db, out, v["id"]) == "not_needed"
    if video.h264_encoder():
        assert video.proxy_state(db, out, old["id"]) == "ok"
        assert video.probe(video.proxy_path(out, old["id"]))["playable"]
    # originals are never changed
    assert tree_hashes(vsrc) == before


@needs_ffmpeg
def test_frame_sampling_and_signature(vsrc):
    p = vsrc / "phone/VID_0001.mp4"
    fr = video.frames(p, video.sample_times(6, 8), max_side=224)
    assert len(fr) == 3 and all(max(im.size) == 224 for _, im in fr)
    a = video.signature(p, 6.0)
    b = video.signature(vsrc / "backup/VID_0001_small.mp4", 6.0)
    c = video.signature(vsrc / "backup/MVI_0002.avi", 3.0)
    assert video.signature_distance(a, b) <= 10
    assert not video.same_video({"duration": 6.0, "sig": a}, {"duration": 3.0, "sig": c})
    assert video.frame(p, 999).size == (640, 360)          # past the end: falls back to the first frame


@needs_ffmpeg
def test_video_web_api_player_and_search(vsrc, tmp_path):
    out = tmp_path / "out"
    run(vsrc, out)
    from fastapi.testclient import TestClient
    from photosorter.web.app import create_app
    c = TestClient(create_app(out))
    db = DB(db_path(out))
    v = _row(db, "VID_0001.mp4")
    for page in ("/videos", f"/watch?id={v['id']}", "/static/videos.js", "/static/watch.js"):
        assert c.get(page).status_code == 200, page
    r = c.get("/api/search?media=video&hide_live=true").json()
    names = {i["filename"] for i in r["items"]}
    assert "VID_0001.mp4" in names and "IMG_0100.mov" not in names and "IMG_0010.jpg" not in names
    assert all(i["media"] == "video" and i["duration"] for i in r["items"])
    allr = c.get("/api/search").json()
    assert {"IMG_0010.jpg", "VID_0001.mp4"} <= {i["filename"] for i in allr["items"]}
    photos = c.get("/api/search?media=photo").json()
    assert all(i["media"] != "video" for i in photos["items"])
    f = c.get("/api/facets").json()
    assert f["ffmpeg"] and f["videos"] >= 3 and f["live_videos"] == 1
    info = c.get(f"/api/video/{v['id']}").json()
    if _h264() == "mpeg4":
        pytest.skip("no H.264 encoder here to make a playable test video")
    assert info["ready"] and info["stream"] and info["sprite"]["n"] >= 5
    data = (out / v["dest_path"]).read_bytes()
    s = c.get(info["stream"], headers={"Range": "bytes=0-99"})
    assert s.status_code == 206 and s.content == data[:100]
    assert c.get(f"/video/{v['id']}/sprite.jpg").headers["content-type"] == "image/jpeg"
    assert c.get(f"/video/{v['id']}/thumbs.vtt").text.startswith("WEBVTT")
    assert c.get(f"/thumb/{v['id']}.jpg").status_code == 200
    assert c.get(f"/video/{v['id']}/original").content == data
    # date and place filters work on videos
    assert c.get("/api/search?media=video&date_from=2022-06-01&date_to=2022-06-30").json()["total"] >= 1
    assert c.get("/api/search?media=video&date_from=2030-01-01").json()["total"] == 0


# ---- Delete on selections ---------------------------------------------------------------------------------
def test_delete_selected_copies_and_undo(sample_src, tmp_path, folder_trash):
    before = tree_hashes(sample_src)
    out = tmp_path / "out"
    run(sample_src, out)
    db = DB(db_path(out))
    a, b = _row(db, "IMG_0002.jpg"), _row(db, "receipt_scan.jpg")
    pa, pb = out / a["dest_path"], out / b["dest_path"]
    from photosorter import albums
    al = albums.create_album(db, out, "Trip")
    albums.add_files(db, out, al["id"], [a["id"]])
    assert pa.exists() and pb.exists()
    from fastapi.testclient import TestClient
    from photosorter.web.app import create_app
    c = TestClient(create_app(out))
    assert c.post("/api/files/delete", json={"file_ids": [a["id"]], "confirm": False}, headers=H).status_code == 400
    assert c.post("/api/files/delete", json={"file_ids": [a["id"]], "confirm": True}).status_code in (400, 403)
    r = c.post("/api/files/delete", json={"file_ids": [a["id"], b["id"]], "confirm": True}, headers=H).json()
    assert r["files"] == 2 and r["removed_copies"] >= 2 and not r["recycle_bin"]
    assert not pa.exists() and not pb.exists()
    assert list((out / "_index").rglob("IMG_0002.jpg"))                        # in the app's trash folder
    assert not list((out / "Albums").rglob("IMG_0002.jpg"))
    ids = {i["id"] for i in c.get("/api/search?limit=200").json()["items"]}
    assert a["id"] not in ids and b["id"] not in ids
    assert tree_hashes(sample_src) == before                                     # originals untouched
    # a sort run doesn't bring them back
    run(sample_src, out)
    assert not pa.exists()
    # verify knows they were deleted on purpose
    res = verify.Verifier(out, run_mode="full").run()
    assert res is not None
    rows = {Path(r["path"]).name: r["bucket"] for r in verify.results(out, limit=1000)}
    assert rows["IMG_0002.jpg"] == "safe_deleted" and rows["receipt_scan.jpg"] == "safe_deleted"
    # undo puts the copies (and the album link) back
    u = c.post("/api/cleanup/undo", json={}, headers=H).json()
    assert not u.get("problems")
    assert pa.exists() and pb.exists() and list((out / "Albums").rglob("IMG_0002.jpg"))
    assert a["id"] in {i["id"] for i in c.get("/api/search?limit=200").json()["items"]}


def test_delete_refuses_paths_outside_the_library(sample_src, tmp_path, folder_trash):
    out = tmp_path / "out"
    run(sample_src, out)
    db = DB(db_path(out))
    a = _row(db, "IMG_0002.jpg")
    db.execute("UPDATE files SET dest_path=? WHERE id=?", (a["src_path"], a["id"]))   # points at an original
    db.commit()
    with pytest.raises(cleanup.CleanupError):
        cleanup.delete_files(db, out, [a["id"]])
    assert Path(a["src_path"]).exists()


@needs_ffmpeg
def test_delete_videos_too(vsrc, tmp_path, folder_trash):
    out = tmp_path / "out"
    run(vsrc, out)
    db = DB(db_path(out))
    v = _row(db, "MVI_0002.avi")
    p = out / v["dest_path"]
    r = cleanup.delete_files(db, out, [v["id"]])
    assert r["files"] == 1 and not p.exists() and (vsrc / "backup/MVI_0002.avi").exists()
    cleanup.undo_last(db, out)
    assert p.exists()


# ---- sync to Family Photos ---------------------------------------------------------------------------------
@needs_ffmpeg
def test_videos_sync_last_chunked_and_checked(vsrc, tmp_path, pi, monkeypatch):  # noqa: F811
    app, url = pi
    out = tmp_path / "out"
    run(vsrc, out)
    db = DB(db_path(out))
    sync_album_of_everything(db, out)
    tok = app.family.new_token()
    sync.update_config(out, {"network_sync": True, "pi_url": url, "token": tok, "max_mbps": 50, "include_full": True})
    monkeypatch.setattr(sync, "CHUNK", 64 * 1024)
    sent = []
    real = sync.Syncer.upload

    def spy(self, name, p, sha, size, offset=0):
        sent.append(name)
        return real(self, name, p, sha, size, offset)
    monkeypatch.setattr(sync.Syncer, "upload", spy)
    res = sync.Syncer(out, run_mode="full").run()
    assert res["ok"], res
    kinds = [n.split(".")[1] for n in sent if ".catalog." not in n]
    assert "v" in kinds and "o" in kinds
    last_photo = max(i for i, k in enumerate(kinds) if k in ("t", "p", "o", "f"))
    first_video = min(i for i, k in enumerate(kinds) if k in ("v", "x"))
    assert first_video > last_photo                                             # videos go last
    v = _row(db, "VID_0001.mp4")
    item = next(i for i in app.family.browse()["items"] if i["id"] == v["id"])
    if _h264() == "mpeg4":
        pytest.skip("no H.264 encoder here to make a playable test video")
    assert item["video"]["src"] and item["video"]["duration"] == pytest.approx(6, abs=0.2)
    name = item["video"]["src"].rsplit("/", 1)[1]
    assert app.family.media_path(name).read_bytes() == (out / v["dest_path"]).read_bytes()
    assert not any(i["id"] == _row(db, "IMG_0100.mov")["id"] for i in app.family.browse()["items"])  # Live clip
    old = _row(db, "MVI_0002.avi")
    if video.proxy_state(db, out, old["id"]) == "ok":
        it = next(i for i in app.family.browse()["items"] if i["id"] == old["id"])
        assert ".x.mp4" in it["video"]["src"] and it["video"]["download"].endswith(".v.avi")
    # nothing changed: nothing is sent again
    sent.clear()
    assert sync.Syncer(out, run_mode="full").run()["ok"]
    assert [n for n in sent if ".catalog." not in n] == []
    # videos switched off: they leave the Pi at the next sync
    sync.update_config(out, {"include_videos": False})
    assert sync.Syncer(out, run_mode="full").run()["ok"]
    assert all(i["video"] is None for i in app.family.browse()["items"])
    assert not app.family.media_path(name).exists()


@needs_ffmpeg
def test_old_family_photos_gets_no_videos(vsrc, tmp_path):
    out = tmp_path / "out"
    run(vsrc, out)
    sync_album_of_everything(DB(db_path(out)), out)
    plan = sync.build_plan(out, {**sync.DEFAULTS, "include_videos": False})
    assert all(r.get("mv") != "video" for r in plan["photos"])
    assert not any(it["kind"] in ("v", "x") for it in plan["items"])
    plan = sync.build_plan(out, {**sync.DEFAULTS})
    assert any(r.get("mv") == "video" for r in plan["photos"])
    assert sync.NAME_RE.match("a" * 64 + ".v.mov") and sync.NAME_RE.match("a" * 64 + ".x.mp4")


def test_video_content_search_with_real_models(models_env, tmp_path):
    """SigLIP 2 over sampled frames: videos are found by what's in them, in the same index as photos."""
    if not video.available():
        pytest.skip("ffmpeg/ffprobe not found")
    src = tmp_path / "msrc"
    src.mkdir()
    for name, color, d in (("red.mp4", "0xdc1414", 4), ("blue.mp4", "0x1414dc", 7)):   # plain frames look alike
        ff("-f", "lavfi", "-i", f"color=c={color}:size=320x240:rate=10:duration={d}", "-c:v", "mpeg4",
           str(src / name))
    from PIL import Image
    Image.new("RGB", (400, 300), (20, 200, 20)).save(src / "green.jpg", quality=90)
    out = tmp_path / "out"
    Pipeline(Settings(sources=[str(src)], output=str(out), use_ai=True, use_faces=True, use_gpu=False,
                      workers=2)).run()
    db = DB(db_path(out))
    assert db.one("SELECT COUNT(*) FROM embeddings e JOIN files f ON f.id=e.file_id WHERE f.media='video'")[0] == 2
    from fastapi.testclient import TestClient
    from photosorter.web.app import create_app
    c = TestClient(create_app(out))
    r = c.get("/api/search", params={"q": "a plain red square"}).json()
    assert r["clip"] and r["items"][0]["filename"] == "red.mp4" and r["items"][0]["media"] == "video"
    r = c.get("/api/search", params={"q": "a plain blue square", "media": "video"}).json()
    assert [i["filename"] for i in r["items"]][0] == "blue.mp4"
    assert all(i["media"] == "video" for i in r["items"])


def test_network_sync_switch_off_by_default(tmp_path):
    """v0.6: 'Sync to Pi over the network' is off unless turned on (existing settings files too); nothing is sent."""
    out = tmp_path / "out"
    (out / "_index").mkdir(parents=True)
    (out / "_index" / "sync.json").write_text('{"pi_url": "https://pi.example.org", "enabled": true}')
    sync.set_token(out, "t" * 43)
    cfg = sync.load_config(out)
    assert cfg["network_sync"] is False and sync.configured(out) and not sync.network_on(out)
    assert "off" in sync.status(out)["line"]
    with pytest.raises(ValueError):
        sync.request_sync(out)
    assert sync.request_after_run(out) is False

    class NoNet:
        def json(self, *a, **k):
            raise AssertionError("must not touch the network while the switch is off")
        request = json

        def close(self):
            pass
    res = sync.Syncer(out, cfg, token="t" * 43, client=NoNet()).run()
    assert res["ok"] is False and res.get("off")
    assert sync.update_config(out, {"network_sync": True})["config"]["network_sync"] is True
    assert sync.network_on(out)
