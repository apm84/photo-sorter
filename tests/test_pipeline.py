import threading
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from photosorter.config import Settings, db_path
from photosorter.db import DB, vec_to_blob
from photosorter.pipeline import Pipeline, build_people_folders, cluster_faces
from photosorter.web.app import create_app

from .conftest import tree_hashes


def run(src, out, **kw):
    s = Settings(sources=[str(src)], output=str(out), use_ai=False, use_faces=False, workers=2, **kw)
    events = []
    summary = Pipeline(s, events.append).run()
    return summary, events


def files_under(out: Path):
    return sorted(str(p.relative_to(out)).replace("\\", "/") for p in out.rglob("*")
                  if p.is_file() and "_index" not in p.parts)


def test_end_to_end_tree(sample_src, tmp_path):
    before = tree_hashes(sample_src)
    out = tmp_path / "out"
    summary, events = run(sample_src, out)
    assert tree_hashes(sample_src) == before, "originals must never change"
    got = files_under(out)
    assert "Photos/2019/2019-07 Seattle, Washington/IMG_0001.jpg" in got
    assert "Photos/2021/2021-12 Denver, Colorado/IMG_0002.jpg" in got
    assert "Photos/2018/2018-03/IMG_20180302_120000.jpg" in got
    assert "Photos/Unknown Date/nodate.jpg" in got
    assert "Screenshots/2020/Screenshot_20200105-101010.png" in got
    assert "Screenshots/Unknown Date/IMG_5512.PNG" in got
    assert "Memes/FB_IMG_1584123.jpg" in got
    assert "Documents/receipt_scan.jpg" in got
    assert "Other/corrupt.jpg" in got
    # near-duplicate (smaller copy) goes to Duplicates for review; exact copy is only listed in the CSV
    dup = [g for g in got if g.startswith("Duplicates/")]
    assert any(g.endswith("IMG_0001_small.jpg") for g in dup) and any(g.endswith("_kept.txt") for g in dup)
    assert not any("IMG_0001 copy.jpg" in g for g in got)
    csv = (out / "_index/reports/exact-duplicates.csv").read_text(encoding="utf-8")
    assert "IMG_0001 copy.jpg" in csv
    assert summary["files"] == 11 and summary["errors"] == 1 and summary["exact_duplicates"] == 1
    assert summary["dup_groups"] == 1
    assert any(e["kind"] == "done" for e in events)
    assert not any(p.name.endswith(".partial") for p in out.rglob("*"))


def test_resume_is_idempotent(sample_src, tmp_path):
    out = tmp_path / "out"
    run(sample_src, out)
    first = tree_hashes(out / "Photos")
    summary, events = run(sample_src, out)
    assert tree_hashes(out / "Photos") == first
    copied = [e for e in events if e["kind"] == "progress" and e["stage"] == "organize"][-1]
    assert copied["copied"] == 0
    # a new file added later is picked up incrementally
    (sample_src / "phone/DCIM/IMG_0001.jpg").read_bytes()
    from .conftest import exif_bytes, scene
    scene(42).save(sample_src / "phone/DCIM/IMG_0100.jpg", exif=exif_bytes("2022:05:01 10:00:00", "Apple", "iPhone"))
    summary, events = run(sample_src, out)
    assert (out / "Photos/2022/2022-05/IMG_0100.jpg").exists()
    assert summary["files"] == 12


def test_copy_exact_duplicates_option(sample_src, tmp_path):
    out = tmp_path / "out"
    run(sample_src, out, copy_exact_duplicates=True)
    assert any(g.endswith("IMG_0001 copy.jpg") and g.startswith("Duplicates/") for g in files_under(out))


def test_stop_and_resume(sample_src, tmp_path):
    out = tmp_path / "out"
    s = Settings(sources=[str(sample_src)], output=str(out), use_ai=False, use_faces=False, workers=1)
    p = Pipeline(s)
    p.pause()
    t = threading.Thread(target=p.run)
    t.start()
    p.stop()
    t.join(10)
    assert not t.is_alive()
    summary, _ = run(sample_src, out)
    assert summary["copied"] == 10  # 11 files minus the byte-identical copy (listed in CSV)
    assert (out / "Photos/2019/2019-07 Seattle, Washington/IMG_0001.jpg").exists()


def test_output_inside_source_is_skipped(sample_src):
    out = sample_src / "sorted"
    summary, _ = run(sample_src, out)
    summary2, _ = run(sample_src, out)
    assert summary2["files"] == summary["files"] == 11


def _fake_faces(out: Path):
    db = DB(db_path(out))
    ids = [r[0] for r in db.query("SELECT id FROM files WHERE type='photo' AND id=rep_id ORDER BY id")]
    rng = np.random.default_rng(0)
    a, b = rng.normal(size=128), rng.normal(size=128)
    for i, fid in enumerate(ids):
        for base in ([a] if i % 2 else [a, b]):
            v = base + rng.normal(scale=0.05, size=128)
            v /= np.linalg.norm(v)
            db.execute("INSERT INTO faces(file_id,x1,y1,x2,y2,score,embedding) VALUES(?,?,?,?,?,?,?)",
                       (fid, .2, .2, .5, .5, .9, vec_to_blob(v)))
    db.commit()
    return db


def test_face_clustering_keeps_names(sample_src, tmp_path):
    out = tmp_path / "out"
    run(sample_src, out)
    db = _fake_faces(out)
    cluster_faces(db, eps=0.3, min_samples=2)
    people = db.query("SELECT id, face_count FROM people ORDER BY face_count DESC")
    assert len(people) == 2
    big = people[0]["id"]
    db.execute("UPDATE people SET name='Alex' WHERE id=?", (big,)); db.commit()
    cluster_faces(db, eps=0.3, min_samples=2)
    names = [r[0] for r in db.query("SELECT name FROM people")]
    assert "Alex" in names and len(names) == 2
    n = build_people_folders(out, db)
    assert n >= 2 and (out / "People/Alex").is_dir()


def test_web_queries(sample_src, tmp_path):
    out = tmp_path / "out"
    run(sample_src, out)
    db = _fake_faces(out)
    cluster_faces(db, eps=0.3, min_samples=2)
    c = TestClient(create_app(out))
    assert "Photo Sorter" in c.get("/").text and c.get("/people").status_code == 200
    f = c.get("/api/facets").json()
    assert {t["value"] for t in f["types"]} >= {"photo", "screenshot", "meme", "document"}
    assert f["clip_search"] is False
    r = c.get("/api/search", params={"type": "screenshot"}).json()
    assert r["total"] == 2
    r = c.get("/api/search", params={"year": "2019"}).json()
    assert [i["filename"] for i in r["items"]] == ["IMG_0001.jpg"]   # duplicates hidden by default
    r = c.get("/api/search", params={"state": "Colorado"}).json()
    assert [i["filename"] for i in r["items"]] == ["IMG_0002.jpg"]
    r = c.get("/api/search", params={"place": "Seattle, Washington"}).json()
    assert r["total"] == 1
    r = c.get("/api/search", params={"duplicates": "true"}).json()
    assert r["total"] == 3
    r = c.get("/api/search", params={"q": "receipt"}).json()   # keyword fallback without AI models
    assert [i["filename"] for i in r["items"]] == ["receipt_scan.jpg"]
    pid = c.get("/api/people").json()[0]["id"]
    assert c.get("/api/search", params={"person": pid}).json()["total"] >= 1
    assert c.post(f"/api/people/{pid}/name", json={"name": "Mom"}).status_code == 403   # header required
    assert c.post(f"/api/people/{pid}/name", json={"name": "Mom"}, headers={"X-PhotoSorter": "1"}).json()["ok"]
    assert any(p["name"] == "Mom" for p in c.get("/api/people").json())
    fid = r["items"][0]["id"]
    t = c.get(f"/thumb/{fid}.jpg")
    assert t.status_code == 200 and t.headers["content-type"] in ("image/webp", "image/jpeg")
    face = db.one("SELECT id FROM faces LIMIT 1")[0]
    assert c.get(f"/face/{face}.jpg").status_code == 200
    assert c.post(f"/api/open/{fid}").status_code == 403
    assert c.post(f"/api/open/{fid}", headers={"X-PhotoSorter": "1"}).json()["path"].endswith("receipt_scan.jpg")


def test_web_rejects_non_local(sample_src, tmp_path):
    out = tmp_path / "out"
    run(sample_src, out)
    c = TestClient(create_app(out), client=("10.0.0.5", 1234))
    assert c.get("/api/facets").status_code == 403
