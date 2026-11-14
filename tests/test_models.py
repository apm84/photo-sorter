"""Runs only when PHOTOSORTER_TEST_MODELS points at a folder with downloaded/exported models."""
import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from photosorter.config import Settings, db_path
from photosorter.db import DB, blob_to_vec
from photosorter.embedders import get_embedder
from photosorter.pipeline import Pipeline
from photosorter.web.app import create_app


def test_siglip2_embeddings(models_env):
    e = get_embedder("siglip2", use_gpu=False)
    assert e is not None
    red, blue = Image.new("RGB", (300, 300), (220, 20, 20)), Image.new("RGB", (300, 300), (20, 20, 220))
    v = e.encode_images([red, blue])
    t = e.encode_texts(["a plain red square", "a plain blue square"])
    assert np.allclose(np.linalg.norm(v, axis=1), 1, atol=1e-3)
    sims = v @ t.T
    assert sims[0, 0] > sims[0, 1] and sims[1, 1] > sims[1, 0]


def test_pipeline_with_ai_and_faces(models_env, sample_src, tmp_path):
    out = tmp_path / "out"
    s = Settings(sources=[str(sample_src)], output=str(out), use_ai=True, use_faces=True, use_gpu=False, workers=2)
    summary = Pipeline(s).run()
    db = DB(db_path(out))
    assert db.one("SELECT COUNT(*) FROM embeddings")[0] == 9   # 11 files - corrupt - exact copy
    assert db.get_meta("embedder") == "siglip2" and db.get_meta("face_backend") == "yunet_sface"
    # resized near-duplicate confirmed by AI similarity
    a, b = (blob_to_vec(r[0]) for r in db.query(
        "SELECT e.vec FROM embeddings e JOIN files f ON f.id=e.file_id WHERE f.filename IN ('IMG_0001.jpg','IMG_0001_small.jpg')"))
    assert float(a @ b) / (np.linalg.norm(a) * np.linalg.norm(b)) > 0.92
    assert summary["dup_groups"] == 1
    types = {r[0]: r[1] for r in db.query("SELECT filename, type FROM files")}
    assert types["Screenshot_20200105-101010.png"] == "screenshot" and types["IMG_0001.jpg"] == "photo"
    c = TestClient(create_app(out))
    assert c.get("/api/facets").json()["clip_search"] is True
    r = c.get("/api/search", params={"q": "a screenshot of a messaging app"}).json()
    assert r["clip"] and r["items"][0]["type"] == "screenshot"
    assert r["items"][0]["score"] >= r["items"][-1]["score"]
