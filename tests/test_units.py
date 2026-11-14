from datetime import datetime

import numpy as np
from PIL import Image

from photosorter import classify as cls
from photosorter.dedupe import UnionFind, near_pairs, pick_keeper
from photosorter.geo import place_label, reverse_geocode, safe_name
from photosorter.imaging import analyze_file, date_from_filename, read_exif

from .conftest import exif_bytes, scene


def test_date_from_filename():
    assert date_from_filename("IMG_20190704_153012.jpg") == datetime(2019, 7, 4, 15, 30, 12)
    assert date_from_filename("PXL_20210101_101010123.jpg").date() == datetime(2021, 1, 1).date()
    assert date_from_filename("Screenshot_20200105-101010.png") == datetime(2020, 1, 5, 10, 10, 10)
    assert date_from_filename("Screen Shot 2019-07-04 at 3.30.12 PM.png") == datetime(2019, 7, 4, 3, 30, 12)
    assert date_from_filename("IMG-20190704-WA0001.jpg").date() == datetime(2019, 7, 4).date()
    assert date_from_filename("2018-03-02 12.00.00.jpg") == datetime(2018, 3, 2, 12, 0, 0)
    assert date_from_filename("IMG_0001.jpg") is None
    assert date_from_filename("photo_99999999.jpg") is None


def test_exif_roundtrip(tmp_path):
    p = tmp_path / "a.jpg"
    scene(1, 400, 300).save(p, exif=exif_bytes("2019:07:04 15:30:12", "Apple", "iPhone 8", (47.6062, -122.3321)))
    with Image.open(p) as im:
        ex = read_exif(im)
    assert ex["taken_at"] == datetime(2019, 7, 4, 15, 30, 12)
    assert ex["camera_make"] == "Apple" and ex["camera_model"] == "iPhone 8"
    assert abs(ex["lat"] - 47.6062) < 1e-3 and abs(ex["lon"] + 122.3321) < 1e-3
    res = analyze_file(p)
    assert res["width"] == 400 and res["height"] == 300 and len(res["sha256"]) == 64


def test_reverse_geocode_offline():
    r = reverse_geocode([(47.6062, -122.3321), (48.8566, 2.3522)])
    assert r[0]["cc"] == "US" and r[0]["state"] == "Washington" and "Seattle" in r[0]["place"]
    assert r[1]["cc"] == "FR" and r[1]["place"].endswith("France")
    assert place_label("Denver", "Colorado", "US") == "Denver, Colorado"


def test_safe_name():
    assert safe_name('a<b>c:"d"/e\\f|g?h*') == "a_b_c__d__e_f_g_h_"
    assert safe_name("CON") == "_CON"
    assert safe_name("trail. ") == "trail"


def test_classify_heuristics():
    f = cls.heuristic_features("Screenshot_2020.png", 1170, 2532, "PNG", None, None)
    assert cls.classify(f)[0] == "screenshot"
    f = cls.heuristic_features("IMG_5512.PNG", 1080, 2400, "PNG", None, None)
    assert cls.classify(f)[0] == "screenshot"
    f = cls.heuristic_features("IMG_0001.jpg", 4032, 3024, "JPEG", "Apple", "iPhone")
    assert cls.classify(f)[0] == "photo"
    # camera EXIF strongly overrides an AI "screenshot" guess
    t, _, _ = cls.classify(f, {"photo": 0.2, "screenshot": 0.6, "meme": 0.1, "document": 0.05, "graphic": 0.05})
    assert t == "photo"
    f = cls.heuristic_features("FB_IMG_1.jpg", 700, 700, "JPEG", None, None)
    assert cls.classify(f, {"photo": 0.35, "screenshot": 0.05, "meme": 0.3, "document": 0.1, "graphic": 0.2})[0] == "meme"
    f = cls.heuristic_features("x.jpg", 1000, 1400, "JPEG", None, None)
    assert cls.classify(f, {"photo": 0.1, "screenshot": 0.1, "meme": 0.1, "document": 0.6, "graphic": 0.1})[0] == "document"


def test_near_pairs_and_keeper():
    ids = [1, 2, 3]
    ph = [0b1111, 0b1110, (1 << 63) | 0xFFFF0000]   # 1 and 2 differ by 1 bit
    dh = [0, 1, 12345]
    pairs = near_pairs(ids, ph, dh, max_p=4, max_d=4)
    assert pairs == [(1, 2, 1)]
    files = [{"id": 1, "width": 800, "height": 600, "size": 10, "src_path": "a"},
             {"id": 2, "width": 1600, "height": 1200, "size": 5, "src_path": "b", "date_source": "exif"}]
    assert pick_keeper(files) == 2
    uf = UnionFind(); uf.union(3, 1); uf.union(2, 3); uf.find(9)
    assert uf.groups() == [[1, 2, 3]]


def test_near_pairs_signed_storage():
    from photosorter.db import to_signed64, to_unsigned64
    big = (1 << 64) - 5
    assert to_unsigned64(to_signed64(big)) == big
    assert near_pairs([1, 2], [to_signed64(big), big], [0, 0], 0, 0) == [(1, 2, 0)]
