"""Duplicate clean-up, schema migration, incremental batches, and face naming."""
import csv
import shutil
import sqlite3
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from photosorter import cleanup
from photosorter.config import Settings, db_path
from photosorter.db import DB, vec_to_blob
from photosorter.pipeline import Pipeline, cluster_faces
from photosorter.web.app import create_app

from .conftest import exif_bytes, scene, tree_hashes

H = {"X-PhotoSorter": "1"}


def run(src, out, **kw):
    s = Settings(sources=[str(src)], output=str(out), use_ai=False, use_faces=False, workers=2, **kw)
    return Pipeline(s).run()


def files_under(out: Path):
    return sorted(str(p.relative_to(out)).replace("\\", "/") for p in out.rglob("*")
                  if p.is_file() and "_index" not in p.parts)


@pytest.fixture
def folder_trash(monkeypatch):
    monkeypatch.setenv("PHOTOSORTER_TRASH", "folder")


def the_group(db):
    gid = db.one("SELECT dup_group FROM files WHERE filename='IMG_0001_small.jpg'")[0]
    d = cleanup.group_detail(db, gid)
    by = {m["filename"]: m for m in d["members"]}
    return gid, d, by


# ---- migration -------------------------------------------------------------------------------------------
V01_DATA = """
INSERT INTO meta VALUES('schema_version', '"1"');
INSERT INTO files(id, src_path, filename, dup_group, is_keeper, dest_path) VALUES(1, 'C:/a.jpg', 'a.jpg', 7, 1, 'Photos/a.jpg');
INSERT INTO dup_groups VALUES(7, 'near', 1, 2);
INSERT INTO people(id, name) VALUES(3, 'Grandma');
INSERT INTO faces(id, file_id, person_id) VALUES(5, 1, 3);
"""


def test_migration_from_v01_index(tmp_path):
    p = tmp_path / "old.sqlite"
    c = sqlite3.connect(p); c.executescript((Path(__file__).parent / "data/schema_v01.sql").read_text() + V01_DATA); c.commit(); c.close()
    db = DB(p)
    cols = lambda t: {r[1] for r in db.query(f"PRAGMA table_info({t})")}
    assert {"suggested_keeper_id", "note"} <= cols("dup_groups")
    assert {"locked", "not_person"} <= cols("faces")
    assert {"file_id", "state", "action_id"} <= cols("file_review") and "ops" in cols("review_actions")
    assert db.get_meta("schema_version") >= 3   # v0.4 is schema 4
    assert db.one("SELECT keeper_id FROM dup_groups WHERE id=7")[0] == 1          # data kept
    assert db.one("SELECT name FROM people WHERE id=3")[0] == "Grandma"
    assert db.one("SELECT locked FROM faces WHERE id=5")[0] == 0
    db.close()
    DB(p).close()                                                                 # idempotent


# ---- applying decisions ----------------------------------------------------------------------------------
def test_approve_recommendation_and_undo(sample_src, tmp_path, folder_trash):
    before = tree_hashes(sample_src)
    out = tmp_path / "out"
    run(sample_src, out)
    db = DB(db_path(out))
    gid, d, by = the_group(db)
    assert d["recommended_keeper_id"] == by["IMG_0001.jpg"]["id"]
    assert any("higher resolution" in r for r in d["reasons"])
    assert by["IMG_0001 copy.jpg"]["location"] == "not copied" and by["IMG_0001_small.jpg"]["location"] == "duplicates"
    small_dest = by["IMG_0001_small.jpg"]["dest_path"]
    keep = {m["id"] for m in d["members"] if m["proposed"] == "keep"}
    assert keep == {by["IMG_0001.jpg"]["id"]}
    assert cleanup.summary(db)["reviewed"] == 0
    cleanup.apply_decision(db, out, gid, keep)
    assert tree_hashes(sample_src) == before, "originals must never change"
    assert not (out / small_dest).exists()
    assert not (out / small_dest).parent.exists(), "emptied Duplicates group folder is tidied away"
    trashed = list((out / "_index" / "_trash").rglob("IMG_0001_small.jpg"))
    assert len(trashed) == 1
    assert (out / "Photos/2019/2019-07 Seattle, Washington/IMG_0001.jpg").exists()
    s = cleanup.summary(db)
    assert s["reviewed"] == s["total"] == 1 and s["pending"] == 0 and s["last_action"]
    rows = list(csv.DictReader(open(out / "_index/reports/exact-duplicates.csv", encoding="utf-8")))
    assert rows and rows[0]["review"] == "confirmed duplicate"
    # undo puts everything back
    r = cleanup.undo_last(db, out)
    assert r["problems"] == []
    assert (out / small_dest).exists() and not trashed[0].exists()
    assert cleanup.summary(db)["reviewed"] == 0
    assert db.one("SELECT COUNT(*) FROM file_review")[0] == 0
    rows = list(csv.DictReader(open(out / "_index/reports/exact-duplicates.csv", encoding="utf-8")))
    assert rows[0]["review"] == "not reviewed"
    assert tree_hashes(sample_src) == before
    with pytest.raises(cleanup.CleanupError):
        cleanup.undo_last(db, out)


def test_recycle_bin_is_used_when_available(sample_src, tmp_path, monkeypatch):
    monkeypatch.delenv("PHOTOSORTER_TRASH", raising=False)
    bin_dir = tmp_path / "RecycleBin"; bin_dir.mkdir()
    sent = []

    def fake_send2trash(p):
        sent.append(p); shutil.move(p, bin_dir / Path(p).name)

    monkeypatch.setitem(sys.modules, "send2trash", types.SimpleNamespace(send2trash=fake_send2trash))
    before = tree_hashes(sample_src)
    out = tmp_path / "out"
    run(sample_src, out)
    db = DB(db_path(out))
    gid, d, by = the_group(db)
    cleanup.apply_decision(db, out, gid, [by["IMG_0001.jpg"]["id"]])
    assert len(sent) == 1 and sent[0].endswith("IMG_0001_small.jpg")
    assert not (out / "_index" / "_trash").exists()
    # undo after recycling: restored by copying the untouched original back into the library
    cleanup.undo_last(db, out)
    restored = out / by["IMG_0001_small.jpg"]["dest_path"]
    assert restored.read_bytes() == (sample_src / "backup/old/IMG_0001_small.jpg").read_bytes()
    assert tree_hashes(sample_src) == before


def test_keeper_swap_moves_new_keeper_into_library(sample_src, tmp_path, folder_trash):
    before = tree_hashes(sample_src)
    out = tmp_path / "out"
    run(sample_src, out)
    db = DB(db_path(out))
    gid, d, by = the_group(db)
    small = by["IMG_0001_small.jpg"]["id"]
    cleanup.apply_decision(db, out, gid, [small], small)
    lib = out / "Photos/2019/2019-07 Seattle, Washington"
    assert (lib / "IMG_0001_small.jpg").exists(), "new keeper goes to the old keeper's library folder"
    assert not (lib / "IMG_0001.jpg").exists(), "old keeper moved out (to trash)"
    assert list((out / "_index/_trash").rglob("IMG_0001.jpg"))
    assert db.one("SELECT keeper_id FROM dup_groups WHERE id=?", (gid,))[0] == small
    assert db.one("SELECT dest_path FROM files WHERE id=?", (small,))[0].replace("\\", "/") == \
        "Photos/2019/2019-07 Seattle, Washington/IMG_0001_small.jpg"
    assert tree_hashes(sample_src) == before
    cleanup.undo_last(db, out)
    assert (lib / "IMG_0001.jpg").exists() and not (lib / "IMG_0001_small.jpg").exists()
    assert (out / by["IMG_0001_small.jpg"]["dest_path"]).exists()
    assert tree_hashes(sample_src) == before


def test_keep_all_does_not_copy_identical_original(sample_src, tmp_path, folder_trash):
    before = tree_hashes(sample_src)
    out = tmp_path / "out"
    run(sample_src, out)
    db = DB(db_path(out))
    gid, d, by = the_group(db)
    cleanup.apply_decision(db, out, gid, [m["id"] for m in d["members"]])
    got = files_under(out)
    assert "Photos/2019/2019-07 Seattle, Washington/IMG_0001.jpg" in got
    assert "Photos/2019/2019-07 Seattle, Washington/IMG_0001 copy.jpg" not in got
    assert not any(" (1)" in g or "(1)" in Path(g).stem for g in got)
    assert any(g.startswith("Photos/") and g.endswith("IMG_0001_small.jpg") for g in got)
    assert not any(g.startswith("Duplicates/") for g in got)
    copy_id = by["IMG_0001 copy.jpg"]["id"]
    assert db.one("SELECT dest_path FROM files WHERE id=?", (copy_id,))[0] is None
    assert db.one("SELECT state FROM file_review WHERE file_id=?", (copy_id,))[0] == "keep"
    rows = list(csv.DictReader(open(out / "_index/reports/exact-duplicates.csv", encoding="utf-8")))
    assert any(r["review"] == "kept" and r["duplicate_original_path"].endswith("IMG_0001 copy.jpg") for r in rows)
    assert tree_hashes(sample_src) == before
    cleanup.undo_last(db, out)
    assert "Photos/2019/2019-07 Seattle, Washington/IMG_0001 copy.jpg" not in files_under(out)
    assert db.one("SELECT state FROM file_review WHERE file_id=?", (copy_id,)) is None
    assert tree_hashes(sample_src) == before


def test_refuses_to_touch_originals(sample_src, tmp_path, folder_trash):
    before = tree_hashes(sample_src)
    out = tmp_path / "out"
    run(sample_src, out)
    db = DB(db_path(out))
    gid, d, by = the_group(db)
    small = by["IMG_0001_small.jpg"]
    db.execute("UPDATE files SET dest_path=? WHERE id=?", (small["src_path"], small["id"])); db.commit()  # absolute
    with pytest.raises(cleanup.CleanupError):
        cleanup.apply_decision(db, out, gid, [by["IMG_0001.jpg"]["id"]])
    db.execute("UPDATE files SET dest_path=? WHERE id=?", ("../src/backup/old/IMG_0001_small.jpg", small["id"])); db.commit()
    with pytest.raises(cleanup.CleanupError):
        cleanup.apply_decision(db, out, gid, [by["IMG_0001.jpg"]["id"]])
    assert tree_hashes(sample_src) == before
    assert (out / "Photos/2019/2019-07 Seattle, Washington/IMG_0001.jpg").exists(), "failed action rolled back"
    with pytest.raises(cleanup.CleanupError):
        cleanup.apply_decision(db, out, gid, [])                                   # must keep one


def test_cleanup_web_api(sample_src, tmp_path, folder_trash):
    before = tree_hashes(sample_src)
    out = tmp_path / "out"
    run(sample_src, out)
    c = TestClient(create_app(out))
    assert c.get("/cleanup").status_code == 200 and c.get("/static/cleanup.js").status_code == 200
    s = c.get("/api/cleanup/summary").json()
    assert s["total"] == 1 and s["pending"] == 1 and s["recycle_bin"] is False
    gid = c.get("/api/cleanup/groups").json()["ids"][0]
    g = c.get(f"/api/cleanup/group/{gid}").json()
    assert len(g["members"]) == 3 and g["reasons"]
    assert c.get(f"/preview/{g['members'][0]['id']}.jpg").headers["content-type"] in ("image/webp", "image/jpeg")
    keep = [m["id"] for m in g["members"] if m["proposed"] == "keep"]
    assert c.post(f"/api/cleanup/group/{gid}", json={"keep": keep}).status_code == 403          # header required
    assert c.post("/api/cleanup/approve-all", json={"confirm": False}, headers=H).status_code == 400
    p = c.get("/api/cleanup/preview-all").json()
    assert p["groups"] == 1 and p["remove"] == 2 and p["copies_to_remove"] == 1
    r = c.post("/api/cleanup/approve-all", json={"confirm": True}, headers=H).json()
    assert r["groups"] == 1 and not r["errors"]
    assert c.get("/api/cleanup/summary").json()["pending"] == 0
    assert c.get("/api/cleanup/groups").json()["ids"] == []
    assert c.post("/api/cleanup/undo", headers=H).json()["problems"] == []
    assert c.get("/api/cleanup/summary").json()["pending"] == 1
    assert c.post(f"/api/cleanup/group/{gid}", json={"keep": []}, headers=H).status_code == 409
    assert tree_hashes(sample_src) == before


# ---- incremental batches into one output folder ----------------------------------------------------------
def test_two_batches_keep_group_ids_and_flag_better_copy(tmp_path, folder_trash):
    src = tmp_path / "src"; b1 = src / "batch1"; b1.mkdir(parents=True)
    ex = exif_bytes("2019:07:04 15:30:12", "Apple", "iPhone 8", (47.6062, -122.3321))
    big = scene(1)
    big.resize((800, 600)).save(b1 / "AAA_small.jpg", quality=80, exif=ex)      # only the small copy at first
    s7 = scene(7)
    (b1 / "later").mkdir()
    s7.save(b1 / "later/ZZZ_7.jpg", quality=92, exif=exif_bytes("2020:01:02 10:00:00", "Canon", "EOS"))
    s7.resize((700, 525)).save(b1 / "later/ZZZ_7_small.jpg", quality=70)
    out = tmp_path / "out"
    run(src, out)
    db = DB(db_path(out))
    g7 = db.one("SELECT dup_group FROM files WHERE filename='ZZZ_7_small.jpg'")[0]
    first_dup = files_under(out)
    assert f"Duplicates/group-{g7:05d}/ZZZ_7_small.jpg" in first_dup
    assert "Photos/2019/2019-07 Seattle, Washington/AAA_small.jpg" in first_dup
    # batch 2: the full-size original of AAA arrives. AAA_small was indexed before the ZZZ files, which used
    # to renumber the ZZZ group (v0.1 numbered groups by lowest file id every run).
    b2 = src / "batch2"; b2.mkdir()
    big.save(b2 / "AAA.jpg", quality=92, exif=ex)
    run(src, out)
    assert db.one("SELECT dup_group FROM files WHERE filename='ZZZ_7_small.jpg'")[0] == g7, "group ids stay stable"
    ga = db.one("SELECT dup_group FROM files WHERE filename='AAA.jpg'")[0]
    assert ga != g7 and ga > g7
    got = files_under(out)
    assert f"Duplicates/group-{g7:05d}/ZZZ_7_small.jpg" in got
    assert "Photos/2019/2019-07 Seattle, Washington/AAA_small.jpg" in got, "keeper not swapped silently"
    assert "Photos/2019/2019-07 Seattle, Washington/AAA.jpg" not in got
    assert f"Duplicates/group-{ga:05d}/AAA.jpg" in got, "better newcomer waits in Duplicates"
    g = cleanup.group_detail(db, ga)
    small_id = db.one("SELECT id FROM files WHERE filename='AAA_small.jpg'")[0]
    big_id = db.one("SELECT id FROM files WHERE filename='AAA.jpg'")[0]
    assert g["keeper_id"] == small_id and g["suggested_keeper_id"] == big_id and g["recommended_keeper_id"] == big_id
    assert ga == cleanup.pending_group_ids(db)[0], "needs-review groups come first"
    assert cleanup.summary(db)["needs_review"] == 1
    # approving the suggestion swaps them, keeping the same library folder
    keep = {m["id"] for m in g["members"] if m["proposed"] == "keep"}
    assert keep == {big_id}
    cleanup.apply_decision(db, out, ga, keep)
    got = files_under(out)
    assert "Photos/2019/2019-07 Seattle, Washington/AAA.jpg" in got
    assert "Photos/2019/2019-07 Seattle, Washington/AAA_small.jpg" not in got
    # a third run changes nothing: same ids, decided keeper kept, no new suggestion, no re-copies
    before = tree_hashes(out / "Photos")
    run(src, out)
    assert tree_hashes(out / "Photos") == before
    assert db.one("SELECT dup_group FROM files WHERE filename='AAA.jpg'")[0] == ga
    assert db.one("SELECT keeper_id, suggested_keeper_id FROM dup_groups WHERE id=?", (ga,))[:] == (big_id, None)
    assert db.one("SELECT dup_group FROM files WHERE filename='ZZZ_7_small.jpg'")[0] == g7
    assert ga not in cleanup.pending_group_ids(db)



def test_delete_all_and_undo(sample_src, tmp_path, folder_trash):
    before = tree_hashes(sample_src)
    out = tmp_path / "out"
    run(sample_src, out)
    db = DB(db_path(out))
    gid, d, by = the_group(db)
    small_dest = by["IMG_0001_small.jpg"]["dest_path"]
    keeper_dest = by["IMG_0001.jpg"]["dest_path"]
    with pytest.raises(cleanup.CleanupError, match="keep at least one"):
        cleanup.apply_decision(db, out, gid, [])
    r = cleanup.delete_group(db, out, gid)
    assert r["ops"] >= 1
    assert tree_hashes(sample_src) == before, "originals must never change"
    assert not (out / small_dest).exists()
    assert not (out / keeper_dest).exists()
    assert not (out / small_dest).parent.exists(), "emptied Duplicates group folder is tidied away"
    assert list((out / "_index/_trash").rglob("IMG_0001.jpg"))
    assert list((out / "_index/_trash").rglob("IMG_0001_small.jpg"))
    assert db.one("SELECT keeper_id, suggested_keeper_id FROM dup_groups WHERE id=?", (gid,))[:] == (None, None)
    assert gid not in cleanup.pending_group_ids(db)
    assert cleanup.summary(db)["pending"] == 0
    d2 = cleanup.group_detail(db, gid)
    assert d2["keeper_id"] is None and d2["reviewed"] and all(m["proposed"] == "remove" for m in d2["members"])
    assert all(m["dest_path"] is None for m in d2["members"])
    assert by["IMG_0001 copy.jpg"]["location"] == "not copied"
    assert db.one("SELECT state FROM file_review WHERE file_id=?", (by["IMG_0001 copy.jpg"]["id"],))[0] == "remove"
    rows = list(csv.DictReader(open(out / "_index/reports/exact-duplicates.csv", encoding="utf-8")))
    assert rows and all(r["review"] == "confirmed duplicate" for r in rows)
    cleanup.undo_last(db, out)
    assert (out / small_dest).exists() and (out / keeper_dest).exists()
    assert cleanup.summary(db)["pending"] == 1
    assert db.one("SELECT keeper_id FROM dup_groups WHERE id=?", (gid,))[0] == by["IMG_0001.jpg"]["id"]
    assert db.one("SELECT COUNT(*) FROM file_review")[0] == 0
    assert tree_hashes(sample_src) == before


def test_delete_all_web_api(sample_src, tmp_path, folder_trash):
    before = tree_hashes(sample_src)
    out = tmp_path / "out"
    run(sample_src, out)
    c = TestClient(create_app(out))
    gid = c.get("/api/cleanup/groups").json()["ids"][0]
    assert c.post(f"/api/cleanup/group/{gid}/delete-all", json={"confirm": True}).status_code == 403
    assert c.post(f"/api/cleanup/group/{gid}/delete-all", json={"confirm": False}, headers=H).status_code == 400
    assert c.post(f"/api/cleanup/group/{gid}", json={"keep": []}, headers=H).status_code == 409
    r = c.post(f"/api/cleanup/group/{gid}/delete-all", json={"confirm": True}, headers=H).json()
    assert r["ops"] >= 1
    s = c.get("/api/cleanup/summary").json()
    assert s["pending"] == 0 and s["reviewed"] == 1
    g = c.get(f"/api/cleanup/group/{gid}").json()
    assert g["keeper_id"] is None and g["reviewed"]
    assert c.post("/api/cleanup/undo", headers=H).json()["problems"] == []
    assert c.get("/api/cleanup/summary").json()["pending"] == 1
    assert tree_hashes(sample_src) == before


def test_delete_all_group_survives_later_batch(tmp_path, folder_trash):
    src = tmp_path / "src"; b1 = src / "batch1"; b1.mkdir(parents=True)
    ex = exif_bytes("2019:07:04 15:30:12", "Apple", "iPhone 8", (47.6062, -122.3321))
    big = scene(1)
    big.resize((800, 600)).save(b1 / "AAA_small.jpg", quality=80, exif=ex)
    big.resize((400, 300)).save(b1 / "AAA_tiny.jpg", quality=60, exif=ex)
    out = tmp_path / "out"
    run(src, out)
    db = DB(db_path(out))
    gid = db.one("SELECT dup_group FROM files WHERE filename='AAA_small.jpg'")[0]
    assert gid and db.one("SELECT COUNT(*) FROM files WHERE dup_group=?", (gid,))[0] == 2
    cleanup.delete_group(db, out, gid)
    assert db.one("SELECT keeper_id FROM dup_groups WHERE id=?", (gid,))[0] is None
    assert gid not in cleanup.pending_group_ids(db)
    b2 = src / "batch2"; b2.mkdir()
    big.save(b2 / "AAA.jpg", quality=92, exif=ex)
    run(src, out)
    assert db.one("SELECT dup_group FROM files WHERE filename='AAA.jpg'")[0] == gid, "group id stays"
    assert db.one("SELECT dup_group FROM files WHERE filename='AAA_small.jpg'")[0] == gid
    big_id = db.one("SELECT id FROM files WHERE filename='AAA.jpg'")[0]
    small_id = db.one("SELECT id FROM files WHERE filename='AAA_small.jpg'")[0]
    d = cleanup.group_detail(db, gid)
    assert d["keeper_id"] == big_id and d["recommended_keeper_id"] == big_id
    assert db.one("SELECT state FROM file_review WHERE file_id=?", (small_id,))[0] == "remove"
    got = files_under(out)
    assert "Photos/2019/2019-07 Seattle, Washington/AAA.jpg" in got
    assert "Photos/2019/2019-07 Seattle, Washington/AAA_small.jpg" not in got
    assert gid in cleanup.pending_group_ids(db), "new unmatched review on the new photo waits for a look"
    cleanup.apply_decision(db, out, gid, [big_id])
    assert gid not in cleanup.pending_group_ids(db)
    assert "Photos/2019/2019-07 Seattle, Washington/AAA.jpg" in files_under(out)


def test_undo_rebuilds_people_folders(sample_src, tmp_path, folder_trash):
    from photosorter.pipeline import build_people_folders

    out = tmp_path / "out"
    run(sample_src, out)
    db = DB(db_path(out))
    gid, d, by = the_group(db)
    keeper = by["IMG_0001.jpg"]
    pid = db.execute("INSERT INTO people(name, face_count) VALUES('Mom', 1)").lastrowid
    db.execute("INSERT INTO faces(file_id,x1,y1,x2,y2,score,person_id) VALUES(?,?,?,?,?,?,?)",
               (keeper["id"], 0.2, 0.2, 0.5, 0.5, 0.9, pid))
    db.commit()
    assert build_people_folders(out, db) >= 1
    link = out / "People" / "Mom" / "IMG_0001.jpg"
    assert link.exists()
    cleanup.delete_group(db, out, gid)
    assert not link.exists()
    cleanup.undo_last(db, out)
    assert link.exists()
    assert (out / keeper["dest_path"]).exists()



# ---- face naming -----------------------------------------------------------------------------------------
def _faces(db, n_a=6, n_b=4, seed=0):
    ids = [r[0] for r in db.query("SELECT id FROM files WHERE analyzed=1 AND id=rep_id ORDER BY id")]
    rng = np.random.default_rng(seed)
    a, b = rng.normal(size=128), rng.normal(size=128)
    made = []
    for i in range(n_a + n_b):
        base = a if i < n_a else b
        v = base + rng.normal(scale=0.05, size=128); v /= np.linalg.norm(v)
        fid = ids[i % len(ids)]
        made.append(db.execute("INSERT INTO faces(file_id,x1,y1,x2,y2,score,embedding) VALUES(?,?,?,?,?,?,?)",
                               (fid, .2, .2, .5, .5, .9, vec_to_blob(v))).lastrowid)
    db.commit()
    return made


@pytest.mark.parametrize("method", ["dbscan", "agglomerative"])
def test_face_clusters_naming_remove_merge(sample_src, tmp_path, method):
    out = tmp_path / "out"
    run(sample_src, out)
    db = DB(db_path(out))
    faces = _faces(db)
    cluster_faces(db, 0.3, 2, method)
    people = db.query("SELECT id, face_count FROM people ORDER BY face_count DESC")
    assert [p["face_count"] for p in people] == [6, 4]
    big, small = people[0]["id"], people[1]["id"]
    c = TestClient(create_app(out))
    assert c.post(f"/api/people/{big}/name", json={"name": "Alex"}, headers=H).json()["ok"]
    # remove a wrong face: it is never put back into Alex by re-grouping
    wrong = faces[0]
    assert c.post(f"/api/faces/{wrong}/remove", headers=H).json()["ok"]
    cluster_faces(db, 0.3, 2, method)
    assert db.one("SELECT person_id FROM faces WHERE id=?", (wrong,))[0] != big
    assert db.one("SELECT name FROM people WHERE id=?", (big,))[0] == "Alex"
    assert db.one("SELECT face_count FROM people WHERE id=?", (big,))[0] == 5
    # assign a face by typing a name (new person)
    r = c.post(f"/api/faces/{wrong}/assign", json={"name": "Visitor"}, headers=H).json()
    assert db.one("SELECT name FROM people WHERE id=?", (r["person"],))[0] == "Visitor"
    # merge the other (unnamed) group into Alex; regroup must not split it again
    assert c.post(f"/api/people/{small}/merge", json={"into": big}, headers=H).json()["ok"]
    r = c.post("/api/people/regroup", json={"eps": 0.3, "method": method, "min_samples": 2}, headers=H).json()
    assert r["ok"]
    assert db.one("SELECT face_count FROM people WHERE id=?", (big,))[0] == 9
    assert c.get("/api/people/regroup").json()["method"] == method
    assert c.get(f"/api/people/{big}/faces").json()["total"] == 9
    # search by person name: exact name -> person filter; partial -> keyword match; suggestions
    r = c.get("/api/search", params={"q": "alex"}).json()
    assert r["person"] == big and r["total"] >= 1
    assert c.get("/api/search", params={"q": "Ale"}).json()["total"] >= 1
    s = c.get("/api/suggest", params={"q": "al"}).json()
    assert s["people"][0]["label"] == "Alex"
    s = c.get("/api/suggest", params={"q": "seattle"}).json()
    assert s["places"] and s["places"][0]["value"] == "Seattle, Washington"


def test_names_survive_reindexing(sample_src, tmp_path):
    out = tmp_path / "out"
    run(sample_src, out)
    db = DB(db_path(out))
    _faces(db)
    s = Settings(sources=[str(sample_src)], output=str(out), use_ai=False, use_faces=False, workers=2)
    Pipeline(s).stage_people()
    big = db.one("SELECT id FROM people ORDER BY face_count DESC")[0]
    db.execute("UPDATE people SET name='Mom' WHERE id=?", (big,)); db.commit()
    _faces(db, 3, 0, seed=0)          # more faces of the same person appear in a later batch
    run(sample_src, out)
    Pipeline(s).stage_people()
    r = db.one("SELECT id, face_count FROM people WHERE name='Mom'")
    assert r and r["id"] == big and r["face_count"] == 9


def test_cleanup_waits_while_a_batch_runs(sample_src, tmp_path, folder_trash):
    import time

    out = tmp_path / "out"
    run(sample_src, out)
    db = DB(db_path(out))
    assert db.get_meta("pipeline_heartbeat") is None, "cleared when the run ends"
    gid, d, by = the_group(db)
    db.set_meta("pipeline_heartbeat", time.time())
    assert cleanup.summary(db)["batch_running"]
    with pytest.raises(cleanup.CleanupError, match="running"):
        cleanup.apply_decision(db, out, gid, [by["IMG_0001.jpg"]["id"]])
    db.set_meta("pipeline_heartbeat", time.time() - 600)       # stale (crashed run) does not block forever
    cleanup.apply_decision(db, out, gid, [by["IMG_0001.jpg"]["id"]])


@pytest.mark.parametrize("lock_named", [False, True])
def test_second_batch_new_folder_spans_all_photos_and_keeps_names(tmp_path, folder_trash, lock_named):
    """Batch 2 points at a NEW folder only. Duplicates still match photos from batch 1, the clean-up works on
    batch-1 copies, and new faces go to people named after batch 1 (names never re-clustered away)."""
    b1, b2 = tmp_path / "card1", tmp_path / "card2"
    b1.mkdir(); b2.mkdir()
    ex = exif_bytes("2021:12:25 09:00:00", "Canon", "EOS 80D", (39.7392, -104.9903))
    orig = scene(11)
    orig.resize((800, 600)).save(b1 / "xmas_small.jpg", quality=80, exif=ex)
    for k in range(3):
        scene(20 + k).save(b1 / f"family_{k}.jpg", quality=90, exif=exif_bytes(f"2021:12:2{k} 10:00:00", "Canon", "EOS"))
    out = tmp_path / "out"
    s1 = Settings(sources=[str(b1)], output=str(out), use_ai=False, use_faces=False, workers=2)
    Pipeline(s1).run()
    db = DB(db_path(out))
    rng = np.random.default_rng(5)
    mom = rng.normal(size=128)

    def add_face(fname, base, noise=0.05):
        v = base + rng.normal(scale=noise, size=128); v /= np.linalg.norm(v)
        fid = db.one("SELECT id FROM files WHERE filename=?", (fname,))[0]
        db.execute("INSERT INTO faces(file_id,x1,y1,x2,y2,score,embedding) VALUES(?,?,?,?,?,?,?)",
                   (fid, .2, .2, .5, .5, .9, vec_to_blob(v)))
        db.commit()

    for k in range(3):
        add_face(f"family_{k}.jpg", mom)
    Pipeline(s1).stage_people()
    pid = db.one("SELECT id FROM people")[0]
    db.execute("UPDATE people SET name='Mom' WHERE id=?", (pid,))
    if lock_named:            # e.g. Mom's group was built by merges: her faces are locked, new face must still find her
        db.execute("UPDATE faces SET locked=1 WHERE person_id=?", (pid,))
    db.commit()
    before_b1 = tree_hashes(b1)

    # batch 2: only the new card is selected
    orig.save(b2 / "xmas.jpg", quality=92, exif=ex)                          # better copy of a batch-1 photo
    scene(30).save(b2 / "visit.jpg", quality=90, exif=exif_bytes("2022:03:01 10:00:00", "Canon", "EOS"))
    scene(31).save(b2 / "stranger.jpg", quality=90, exif=exif_bytes("2022:03:02 10:00:00", "Canon", "EOS"))
    s2 = Settings(sources=[str(b2)], output=str(out), use_ai=False, use_faces=False, workers=2)
    Pipeline(s2).run(stages=["scan", "analyze", "geocode", "embed", "classify", "faces"])
    add_face("visit.jpg", mom)
    add_face("stranger.jpg", rng.normal(size=128))
    Pipeline(s2).run(stages=["people", "dedupe", "organize"])

    assert db.one("SELECT COUNT(*) FROM files")[0] == 7, "batch-1 photos stay in the index"
    visit_face = db.one("SELECT fa.person_id FROM faces fa JOIN files f ON f.id=fa.file_id WHERE f.filename='visit.jpg'")[0]
    assert visit_face == pid and db.one("SELECT name FROM people WHERE id=?", (pid,))[0] == "Mom"
    assert db.one("SELECT face_count FROM people WHERE id=?", (pid,))[0] == 4
    stranger = db.one("SELECT fa.person_id FROM faces fa JOIN files f ON f.id=fa.file_id WHERE f.filename='stranger.jpg'")[0]
    assert stranger != pid
    c = TestClient(create_app(out))
    assert c.get("/api/search", params={"q": "Mom"}).json()["total"] == 4

    # duplicate found across batches, though batch 1's folder was not selected this time
    g = db.one("SELECT dup_group FROM files WHERE filename='xmas.jpg'")[0]
    assert g and db.one("SELECT dup_group FROM files WHERE filename='xmas_small.jpg'")[0] == g
    d = cleanup.group_detail(db, g)
    small_id = db.one("SELECT id FROM files WHERE filename='xmas_small.jpg'")[0]
    big_id = db.one("SELECT id FROM files WHERE filename='xmas.jpg'")[0]
    assert d["keeper_id"] == small_id and d["recommended_keeper_id"] == big_id
    assert g in cleanup.pending_group_ids(db)
    cleanup.apply_decision(db, out, g, [big_id])
    got = files_under(out)
    assert "Photos/2021/2021-12 Denver, Colorado/xmas.jpg" in got
    assert "Photos/2021/2021-12 Denver, Colorado/xmas_small.jpg" not in got
    assert tree_hashes(b1) == before_b1, "batch-1 originals untouched"
    assert (b2 / "xmas.jpg").exists()
