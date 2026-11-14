"""v0.3: face grouping past 8k faces, quality, learning from corrections, unreadable report, date filter,
albums (DB + hard-link folders), clean-up tasks, and the v0.2.1 -> v0.3 index migration."""
import csv
import os
import shutil
import sqlite3
import sys
import time
import tracemalloc
import types
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from photosorter import albums, cleanup
from photosorter.config import Settings, db_path
from photosorter.db import DB, vec_to_blob
from photosorter.facegroup import cluster_faces, cluster_labels, face_cluster_params, upgma_labels
from photosorter.pipeline import Pipeline
from photosorter.web.app import create_app

from .conftest import scene, tree_hashes

H = {"X-PhotoSorter": "1"}


def run(src, out, **kw):
    s = Settings(sources=[str(src)], output=str(out), use_ai=False, use_faces=False, workers=2, **kw)
    return Pipeline(s).run()


@pytest.fixture
def folder_trash(monkeypatch):
    monkeypatch.setenv("PHOTOSORTER_TRASH", "folder")


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    monkeypatch.delenv("PHOTOSORTER_TRASH", raising=False)
    bin_dir = tmp_path / "RecycleBin"; bin_dir.mkdir()

    def fake_send2trash(p):
        shutil.move(p, bin_dir / Path(p).name)

    monkeypatch.setitem(sys.modules, "send2trash", types.SimpleNamespace(send2trash=fake_send2trash))
    return bin_dir


def people_vectors(n_people, per, sigma, seed=0, d=128):
    rng = np.random.default_rng(seed)
    C = rng.normal(size=(n_people, d))
    C /= np.linalg.norm(C, axis=1, keepdims=True)
    lab = np.repeat(np.arange(n_people), per)
    X = C[lab] + rng.normal(scale=sigma, size=(len(lab), d))
    return X / np.linalg.norm(X, axis=1, keepdims=True), lab, C


def add_faces(db, vecs, file_ids, **cols):
    ids = []
    for i, v in enumerate(vecs):
        fields = {"file_id": file_ids[i % len(file_ids)], "x1": .2, "y1": .2, "x2": .5, "y2": .5, "score": .9,
                  "embedding": vec_to_blob(v), **cols}
        ids.append(db.execute(f"INSERT INTO faces({','.join(fields)}) VALUES({','.join('?' * len(fields))})",
                              tuple(fields.values())).lastrowid)
    db.commit()
    return ids


@pytest.fixture
def lib(sample_src, tmp_path):
    out = tmp_path / "out"
    run(sample_src, out)
    db = DB(db_path(out))
    fids = [r[0] for r in db.query("SELECT id FROM files WHERE analyzed=1 AND id=rep_id ORDER BY id")]
    return out, db, fids


# ---- 1. grouping ------------------------------------------------------------------------------------------
def test_upgma_matches_sklearn_average_linkage():
    from sklearn.cluster import AgglomerativeClustering
    from sklearn.metrics import adjusted_rand_score

    X, _, _ = people_vectors(30, 15, 0.09, seed=3)
    X = np.vstack([X, np.random.default_rng(1).normal(size=(60, 128))])   # plus loners
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    for thr in (0.3, 0.45, 0.6):
        a = upgma_labels(X, thr)
        b = AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average",
                                    distance_threshold=thr).fit_predict(X)
        assert adjusted_rand_score(a, b) == 1.0


def test_agglomerative_past_8000_faces_without_dbscan(lib, monkeypatch):
    """9,000 faces (over the old 8,000 cap): average linkage still runs (no DBSCAN fallback), finds every
    person, and never builds an n x n matrix (9,000^2 floats would be 324+ MB)."""
    out, db, fids = lib
    import sklearn.cluster

    def no_dbscan(*a, **k):
        raise AssertionError("DBSCAN must not be used for agglomerative grouping")

    monkeypatch.setattr(sklearn.cluster, "DBSCAN", no_dbscan)
    X, lab, _ = people_vectors(60, 150, 0.06, seed=7)
    assert len(X) == 9000
    tracemalloc.start()
    t = time.time()
    labels = cluster_labels(X, 0.45, 2, "agglomerative")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(set(labels.tolist())) == 60 and -1 not in labels
    from sklearn.metrics import adjusted_rand_score
    assert adjusted_rand_score(labels, lab) == 1.0
    assert peak < 120 * 2**20, f"peak memory {peak / 2**20:.0f} MB"
    assert time.time() - t < 120
    # through the database as well
    add_faces(db, X, fids)
    st = cluster_faces(db, 0.45, 2, "agglomerative")
    assert st["method"] == "agglomerative" and st["grouped"] == 9000
    assert db.one("SELECT COUNT(*) FROM people")[0] == 60
    assert db.one("SELECT MAX(face_count) FROM people")[0] == 150


def test_default_method_is_agglomerative(lib):
    out, db, fids = lib
    eps, ms, method = face_cluster_params(db, Settings())
    assert method == "agglomerative" and abs(eps - 0.45) < 1e-9
    old = Settings.from_dict({"face_cluster_method": "dbscan", "use_ai": False})   # settings saved by 0.2
    assert old.face_cluster_method == "agglomerative" and old.use_ai is False
    with pytest.raises(ValueError):
        cluster_labels(np.eye(3, dtype=np.float32), 0.4, 1, "chaining")


def test_low_quality_faces_skipped_but_confident_ones_join_named(lib):
    out, db, fids = lib
    X, lab, C = people_vectors(2, 8, 0.05, seed=11)
    good = add_faces(db, X, fids, face_px=120.0, blur=300.0)
    cluster_faces(db, 0.45, 2)
    mom = db.one("SELECT person_id FROM faces WHERE id=?", (good[0],))[0]
    db.execute("UPDATE people SET name='Mom' WHERE id=?", (mom,)); db.commit()
    rng = np.random.default_rng(2)
    near = C[0] + rng.normal(scale=0.02, size=128); near /= np.linalg.norm(near)
    far = C[0] + rng.normal(scale=0.2, size=128); far /= np.linalg.norm(far)      # sim ~0.4: not confident
    tiny = add_faces(db, [near], fids, face_px=30.0, blur=300.0)[0]
    blurry = add_faces(db, [far], fids, face_px=120.0, blur=5.0)[0]
    unsure = add_faces(db, [near], fids, face_px=120.0, blur=300.0, score=0.5)[0]
    st = cluster_faces(db, 0.45, 2)
    assert st["low_quality"] == 3
    lq = dict(db.query("SELECT id, low_quality FROM faces WHERE id IN (?,?,?)", (tiny, blurry, unsure)))
    assert all(v == 1 for v in lq.values())
    assert db.one("SELECT person_id FROM faces WHERE id=?", (tiny,))[0] == mom      # very confident match
    assert db.one("SELECT person_id FROM faces WHERE id=?", (blurry,))[0] is None   # not confident
    assert db.one("SELECT COUNT(*) FROM faces WHERE low_quality=0")[0] == 16


# ---- 3. learning from corrections -------------------------------------------------------------------------
def test_learning_references_negatives_margin_and_borderline_queue(lib):
    out, db, fids = lib
    c = TestClient(create_app(out))
    rng = np.random.default_rng(5)
    d = 128
    unit = lambda v: v / np.linalg.norm(v)
    young, old, other = (unit(rng.normal(size=d)) for _ in range(3))
    old = unit(young * 0.35 + old)          # same person years later: similar but not close enough to group
    jit = lambda base, s=0.03: unit(base + rng.normal(scale=s, size=d))
    a = add_faces(db, [jit(young) for _ in range(6)], fids)
    b = add_faces(db, [jit(old) for _ in range(6)], fids)
    o = add_faces(db, [jit(other) for _ in range(6)], fids)
    cluster_faces(db, 0.45, 2)
    pa, pb, po = (db.one("SELECT person_id FROM faces WHERE id=?", (x[0],))[0] for x in (a, b, o))
    assert len({pa, pb, po}) == 3
    c.post(f"/api/people/{pa}/name", json={"name": "Dad"}, headers=H)
    c.post(f"/api/people/{po}/name", json={"name": "Uncle"}, headers=H)
    # merging teaches it: both looks of Dad become reference faces (not one average)
    assert c.post(f"/api/people/{pb}/merge", json={"into": pa}, headers=H).json()["ok"]
    assert db.one("SELECT ref_count FROM people WHERE id=?", (pa,))[0] == 12
    new_old = add_faces(db, [jit(old)], fids)[0]
    cluster_faces(db, 0.45, 2)
    assert db.one("SELECT person_id FROM faces WHERE id=?", (new_old,))[0] == pa       # nearest reference
    # margin: a face halfway between Dad and Uncle is not assigned; it goes to the "Is this ...?" queue
    mid = add_faces(db, [unit(young + other)], fids)[0]
    cluster_faces(db, 0.45, 2)
    assert db.one("SELECT person_id FROM faces WHERE id=?", (mid,))[0] is None
    q = c.get("/api/faces/borderline").json()
    item = next(f for f in q["faces"] if f["id"] == mid)
    assert item["name"] in ("Dad", "Uncle") and item["person"] in (pa, po)
    # "Yes" locks it to the person and makes it a reference face
    assert c.post(f"/api/faces/{mid}/confirm", json={"person": item["person"], "yes": True}, headers=H).json()["ok"]
    assert tuple(db.one("SELECT person_id, locked FROM faces WHERE id=?", (mid,))) == (item["person"], 1)
    # "No" adds a negative and the face is never suggested for (or put into) that person again
    mid2 = add_faces(db, [unit(young + other + rng.normal(scale=0.3, size=d))], fids)[0]
    cluster_faces(db, 0.45, 2)
    sp = db.one("SELECT suggest_person FROM faces WHERE id=?", (mid2,))[0]
    if sp is not None:
        assert c.post(f"/api/faces/{mid2}/confirm", json={"person": sp, "yes": False}, headers=H).json()["ok"]
        cluster_faces(db, 0.45, 2)
        assert db.one("SELECT person_id FROM faces WHERE id=?", (mid2,))[0] != sp
        assert db.one("SELECT suggest_person FROM faces WHERE id=?", (mid2,))[0] != sp
    # a removed face is a negative example: a look-alike of it is not added to Dad automatically
    wrong = add_faces(db, [unit(young * 0.7 + other * 0.3)], fids)[0]
    cluster_faces(db, 0.45, 2)
    if db.one("SELECT person_id FROM faces WHERE id=?", (wrong,))[0] == pa:
        assert c.post(f"/api/faces/{wrong}/remove", headers=H).json()["ok"]
    else:
        c.post(f"/api/faces/{wrong}/assign", json={"person": pa}, headers=H)
        assert c.post(f"/api/faces/{wrong}/remove", headers=H).json()["ok"]
    assert db.one("SELECT COUNT(*) FROM face_negatives WHERE face_id=? AND person_id=?", (wrong, pa))[0] == 1
    twin = add_faces(db, [unit(young * 0.7 + other * 0.3 + rng.normal(scale=0.01, size=d))], fids)[0]
    cluster_faces(db, 0.45, 2)
    assert db.one("SELECT person_id FROM faces WHERE id=?", (twin,))[0] != pa
    assert db.one("SELECT person_id FROM faces WHERE id=?", (wrong,))[0] is None       # never put back
    # names, merges and locks survived every re-group
    assert db.one("SELECT name FROM people WHERE id=?", (pa,))[0] == "Dad"
    assert db.one("SELECT COUNT(*) FROM faces WHERE person_id=? AND locked=1", (pa,))[0] >= 12
    named = c.get("/api/people/named").json()
    assert [p["name"] for p in named] == ["Dad", "Uncle"] and named[0]["cover_face"] and named[0]["photos"] >= 1


# ---- 2. unreadable files ----------------------------------------------------------------------------------
def test_unreadable_report_and_log(sample_src, tmp_path, caplog):
    (sample_src / "downloads/clip.jpg").write_bytes(b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  " + b"\x00" * 64)
    (sample_src / "downloads/empty.png").write_bytes(b"")
    out = tmp_path / "out"
    with caplog.at_level("INFO", logger="photosorter"):
        run(sample_src, out)
    rows = list(csv.DictReader(open(out / "_index/reports/unreadable.csv", encoding="utf-8")))
    by = {Path(r["original_path"]).name: r for r in rows}
    assert set(by) == {"clip.jpg", "empty.png", "corrupt.jpg"}
    assert "video" in by["clip.jpg"]["reason"] and "empty" in by["empty.png"]["reason"]
    log = caplog.text
    assert "Unreadable:" in log and "clip.jpg" in log and "HEIC/HEIF support" in log
    assert "unreadable.csv" in log


# ---- 9. date filter ---------------------------------------------------------------------------------------
def test_date_range_filter_search_and_person_gallery(lib):
    out, db, fids = lib
    c = TestClient(create_app(out))
    allr = c.get("/api/search", params={"limit": 1000}).json()
    nodate = [i for i in allr["items"] if not i["taken_at"]]
    assert nodate, "fixture has photos without a date"
    r = c.get("/api/search", params={"date_from": "2019-01-01", "date_to": "2019-12-31"}).json()
    assert [i["filename"] for i in r["items"]] == ["IMG_0001.jpg"]
    r = c.get("/api/search", params={"date_from": "2019-07-04", "date_to": "2019-07-04"}).json()
    assert r["total"] == 1                                   # the 'to' day is included
    r = c.get("/api/search", params={"date_from": "2019-07-05"}).json()
    assert all(i["taken_at"] and i["taken_at"] >= "2019-07-05" for i in r["items"]) and r["total"] >= 1
    assert all(i["taken_at"] for i in c.get("/api/search", params={"date_to": "2030-01-01", "limit": 1000}).json()["items"])
    assert c.get("/api/search", params={"date_from": "July 4"}).status_code == 400
    # person gallery uses the same filter
    X, _, _ = people_vectors(1, 3, 0.03)
    by_name = {r["filename"]: r["id"] for r in db.query("SELECT id, filename FROM files")}
    add_faces(db, X, [by_name["IMG_0001.jpg"], by_name["IMG_0002.jpg"], by_name["nodate.jpg"]])
    cluster_faces(db, 0.45, 2)
    pid = db.one("SELECT id FROM people")[0]
    assert c.get("/api/search", params={"person": pid}).json()["total"] == 3
    r = c.get("/api/search", params={"person": pid, "date_from": "2021-01-01", "date_to": "2021-12-31"}).json()
    assert [i["filename"] for i in r["items"]] == ["IMG_0002.jpg"]
    assert c.get(f"/api/people/{pid}/info").json()["years"][0]["value"] == "2021"
    ids = c.get("/api/search", params={"ids_only": True}).json()
    assert ids["total"] == allr["total"] and len(ids["ids"]) == allr["total"]


# ---- 5. albums --------------------------------------------------------------------------------------------
def test_albums_db_and_hard_link_folders(lib, folder_trash, sample_src):
    out, db, fids = lib
    before = tree_hashes(sample_src)
    c = TestClient(create_app(out))
    by = {r["filename"]: dict(r) for r in db.query("SELECT id, filename, dest_path FROM files")}
    keep = by["IMG_0001.jpg"]; small = by["IMG_0001_small.jpg"]; two = by["IMG_0002.jpg"]
    r = c.post("/api/albums/add", json={"name": "Summer  2019", "file_ids": [keep["id"], two["id"], small["id"]]},
               headers=H).json()
    assert r["added"] == 3 and r["linked"] == 3 and r["copied"] == 0
    aid = r["album"]
    folder = out / "Albums" / "Summer 2019"
    lib_file = out / keep["dest_path"]
    assert os.path.samefile(folder / "IMG_0001.jpg", lib_file)        # hard link, not a copy
    assert c.post("/api/albums", json={"name": "summer 2019"}, headers=H).status_code == 409
    assert c.post("/api/albums/add", json={"album": aid, "file_ids": [keep["id"]]}, headers=H).json()["already"] == 1
    # searchable / filterable
    r = c.get("/api/search", params={"album": aid}).json()
    assert {i["filename"] for i in r["items"]} == {"IMG_0001.jpg", "IMG_0002.jpg", "IMG_0001_small.jpg"}
    assert c.get("/api/search", params={"q": "summer 2019"}).json()["album"] == aid
    assert c.get("/api/suggest", params={"q": "summ"}).json()["albums"][0]["label"] == "Summer 2019"
    assert any(a["label"] == "Summer 2019" for a in c.get("/api/facets").json()["albums"])
    # rename moves the folder
    assert c.post(f"/api/albums/{aid}/rename", json={"name": "Seattle trip"}, headers=H).json()["folder"] == "Seattle trip"
    folder = out / "Albums" / "Seattle trip"
    assert (folder / "IMG_0001.jpg").exists() and not (out / "Albums" / "Summer 2019").exists()
    # remove one photo from the album: only the link goes
    assert c.post(f"/api/albums/{aid}/remove", json={"file_ids": [two["id"]]}, headers=H).json()["removed"] == 1
    assert not (folder / "IMG_0002.jpg").exists() and (out / two["dest_path"]).exists()
    # Clean up removes a copy that is in the album: its album link goes too (space freed); undo brings it back
    gid = db.one("SELECT dup_group FROM files WHERE id=?", (small["id"],))[0]
    cleanup.apply_decision(db, out, gid, [keep["id"]])
    assert not any(p.name.startswith("IMG_0001_small") for p in folder.iterdir())
    assert c.get("/api/albums").json()["albums"][0]["count"] == 1
    cleanup.undo_last(db, out)
    assert any(p.name.startswith("IMG_0001_small") for p in folder.iterdir())
    # delete the album: folder of links goes, photos stay
    assert c.post(f"/api/albums/{aid}/delete", json={"confirm": False}, headers=H).status_code == 400
    r = c.post(f"/api/albums/{aid}/delete", json={"confirm": True}, headers=H).json()
    assert r["deleted"] == aid and not folder.exists()
    assert lib_file.exists() and (out / small["dest_path"]).exists()
    assert c.get("/api/albums").json()["albums"] == []
    assert tree_hashes(sample_src) == before


def test_albums_copy_when_hard_links_fail(lib, monkeypatch):
    out, db, fids = lib

    def no_link(*a, **k):
        raise OSError("hard links not supported on this drive")

    monkeypatch.setattr(albums.os, "link", no_link)
    a = albums.create_album(db, out, "Copies")
    fid = db.one("SELECT id FROM files WHERE filename='IMG_0002.jpg'")[0]
    r = albums.add_files(db, out, a["id"], [fid])
    assert r["copied"] == 1 and "copied" in r["message"]
    assert (out / "Albums" / "Copies" / "IMG_0002.jpg").exists()
    assert db.one("SELECT method FROM album_items")[0] == "copy"


# ---- 11. clean-up tasks -----------------------------------------------------------------------------------
def test_cleanup_tasks_stats_and_empty_trash(lib, folder_trash):
    out, db, fids = lib
    c = TestClient(create_app(out))
    t = {x["id"]: x for x in c.get("/api/cleanup/tasks").json()["tasks"]}
    pv = cleanup.preview_all(db)
    assert t["review"]["files"] == pv["copies_to_remove"] >= 1 and t["review"]["bytes"] == pv["bytes_to_free"] > 0
    assert t["unreadable"]["files"] == 1                       # corrupt.jpg
    assert t["exact"]["files"] == 1 and t["exact"]["bytes"] > 0  # 'IMG_0001 copy.jpg' was not copied
    assert t["trash"]["files"] == 0
    cleanup.approve_all(db, out)                               # Recycle Bin unavailable -> _index/_trash
    t = {x["id"]: x for x in c.get("/api/cleanup/tasks").json()["tasks"]}
    assert t["review"]["files"] == 0 and t["review"]["done"]
    assert t["trash"]["files"] >= 1 and t["trash"]["bytes"] > 0 and t["trash"]["action"]["confirm"] == "permanent"
    assert c.post("/api/cleanup/empty-trash", json={"confirm": False}, headers=H).status_code == 400
    r = c.post("/api/cleanup/empty-trash", json={"confirm": True}, headers=H).json()
    assert r["method"] == "deleted" and r["files"] >= 1
    assert not (out / "_index" / "_trash").exists() and (out / "_index" / "photosorter.sqlite").exists()
    # undo still works after emptying: copies come back from the untouched originals
    cleanup.undo_last(db, out)
    assert {x["id"]: x for x in c.get("/api/cleanup/tasks").json()["tasks"]}["review"]["files"] >= 1


def test_cleanup_tasks_recycle_bin_reminder(lib, fake_bin):
    out, db, fids = lib
    cleanup.approve_all(db, out)
    n = len(list(fake_bin.iterdir()))
    t = {x["id"]: x for x in cleanup.tasks(db, out)["tasks"]}
    assert t["recycle"]["files"] == n >= 1
    assert t["recycle"]["bytes"] == sum(p.stat().st_size for p in fake_bin.iterdir())
    assert TestClient(create_app(out)).post("/api/open-recycle-bin", headers=H).json()["ok"]


# ---- migration --------------------------------------------------------------------------------------------
def test_migration_from_v021_index(tmp_path):
    p = tmp_path / "v021.sqlite"
    c = sqlite3.connect(p)
    c.executescript((Path(__file__).parent / "data/schema_v021.sql").read_text())
    c.executescript("""
        INSERT INTO meta VALUES('schema_version', '"2"');
        INSERT INTO meta VALUES('face_cluster', '{"eps": 0.45, "min_samples": 2, "method": "agglomerative"}');
        INSERT INTO files(id, src_path, filename, width, height, taken_at) VALUES(1, 'C:/a.jpg', 'a.jpg', 4000, 3000, '2020-01-01T10:00:00');
        INSERT INTO people(id, name, face_count) VALUES(3, 'Grandma', 1);
        INSERT INTO faces(id, file_id, x1, y1, x2, y2, score, person_id, locked, not_person) VALUES(5, 1, .1, .1, .12, .12, .9, NULL, 1, 3);
        INSERT INTO faces(id, file_id, x1, y1, x2, y2, score, person_id, locked) VALUES(6, 1, .2, .2, .5, .5, .9, 3, 1);
    """)
    c.commit(); c.close()
    db = DB(p)
    cols = lambda t: {r[1] for r in db.query(f"PRAGMA table_info({t})")}
    assert {"face_px", "blur", "low_quality", "suggest_person", "suggest_sim"} <= cols("faces")
    assert {"refs", "ref_count", "negs"} <= cols("people")
    assert {"album_id", "file_id", "link_path", "method"} <= cols("album_items") and "folder" in cols("albums")
    assert db.get_meta("schema_version") >= 3   # v0.4 is schema 4
    assert tuple(db.one("SELECT face_id, person_id FROM face_negatives")) == (5, 3)   # removal -> negative
    assert db.one("SELECT name FROM people WHERE id=3")[0] == "Grandma"
    assert face_cluster_params(db)[2] == "agglomerative"
    from photosorter.facegroup import backfill_face_px, mark_low_quality
    assert backfill_face_px(db) == 2
    px = dict(db.query("SELECT id, face_px FROM faces"))
    assert round(px[5]) == 24 and round(px[6]) == 360          # at the 1600-px detection scale
    assert mark_low_quality(db) == 1
    db.close()
    DB(p).close()                                              # idempotent


# ---- pages ------------------------------------------------------------------------------------------------
def test_pages_and_static_assets_load(lib):
    out, db, fids = lib
    c = TestClient(create_app(out))
    for page in ("/", "/people", "/cleanup", "/albums"):
        r = c.get(page)
        assert r.status_code == 200 and "Photo Sorter" in r.text and 'href="/albums"' in r.text
        assert "http://" not in r.text and "https://" not in r.text, "no CDN / external scripts"
    for asset in ("common.js", "style.css", "people.js", "cleanup.js", "albums.js", "search.js"):
        assert c.get(f"/static/{asset}").status_code == 200
    fid = fids[0]
    assert c.get(f"/full/{fid}").status_code == 200 and c.get(f"/preview/{fid}.jpg").status_code == 200
    assert c.get("/api/people/regroup").json()["method"] == "agglomerative"
