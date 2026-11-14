import hashlib
import os
import shutil
from pathlib import Path

import numpy as np
import piexif
import pytest
from PIL import Image, ImageDraw


def _deg(v):
    v = abs(v)
    d = int(v); m = int((v - d) * 60); s = round(((v - d) * 60 - m) * 60 * 100)
    return ((d, 1), (m, 1), (s, 100))


def exif_bytes(dt=None, make=None, model=None, gps=None):
    z = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    if make:
        z["0th"][piexif.ImageIFD.Make] = make.encode()
    if model:
        z["0th"][piexif.ImageIFD.Model] = model.encode()
    if dt:
        z["Exif"][piexif.ExifIFD.DateTimeOriginal] = dt.encode()
    if gps:
        lat, lon = gps
        z["GPS"] = {piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S", piexif.GPSIFD.GPSLatitude: _deg(lat),
                    piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W", piexif.GPSIFD.GPSLongitude: _deg(lon)}
    return piexif.dump(z)


def scene(seed, w=1600, h=1200):
    """A random but structured 'photo' (blurred shapes) so perceptual hashes are distinctive."""
    rng = np.random.default_rng(seed)
    im = Image.new("RGB", (w, h), tuple(int(x) for x in rng.integers(0, 255, 3)))
    d = ImageDraw.Draw(im)
    for _ in range(25):
        x0, y0 = rng.integers(0, w), rng.integers(0, h)
        x1, y1 = x0 + rng.integers(50, w // 2), y0 + rng.integers(50, h // 2)
        d.ellipse([x0, y0, x1, y1], fill=tuple(int(x) for x in rng.integers(0, 255, 3)))
    return im


def screenshot_like(w=1170, h=2532):
    im = Image.new("RGB", (w, h), (245, 245, 245))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w, 140], fill=(30, 30, 30))
    for i in range(14):
        y = 200 + i * 160
        d.rounded_rectangle([40 if i % 2 else 300, y, w - (300 if i % 2 else 40), y + 120], 30,
                            fill=(0, 122, 255) if i % 2 == 0 else (229, 229, 234))
    return im


@pytest.fixture
def sample_src(tmp_path) -> Path:
    src = tmp_path / "src"
    (src / "phone" / "DCIM").mkdir(parents=True)
    (src / "backup" / "old").mkdir(parents=True)
    (src / "downloads").mkdir(parents=True)
    p1 = scene(1)
    p1.save(src / "phone/DCIM/IMG_0001.jpg", quality=92,
            exif=exif_bytes("2019:07:04 15:30:12", "Apple", "iPhone 8", (47.6062, -122.3321)))  # Seattle
    shutil.copy2(src / "phone/DCIM/IMG_0001.jpg", src / "backup/old/IMG_0001 copy.jpg")      # exact dup
    p1.resize((800, 600)).save(src / "backup/old/IMG_0001_small.jpg", quality=70)              # near dup
    scene(2).save(src / "phone/DCIM/IMG_0002.jpg", quality=92,
                  exif=exif_bytes("2021:12:25 09:00:00", "Canon", "EOS 80D", (39.7392, -104.9903)))  # Denver
    scene(3).save(src / "phone/DCIM/IMG_20180302_120000.jpg", quality=90)                      # date from name
    scene(4, 900, 900).save(src / "downloads/nodate.jpg", quality=90)                          # no date at all
    screenshot_like().save(src / "phone/Screenshot_20200105-101010.png")
    screenshot_like(1080, 2400).save(src / "downloads/IMG_5512.PNG")                          # screen dims, no name hint
    scene(5, 700, 700).save(src / "downloads/FB_IMG_1584123.jpg", quality=80)                  # meme-ish name
    scene(6, 1000, 1400).save(src / "downloads/receipt_scan.jpg", quality=85)
    (src / "downloads/corrupt.jpg").write_bytes(b"not really a jpeg")
    (src / "downloads/notes.txt").write_text("ignore me")
    return src


def tree_hashes(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def models_env():
    """Point at real downloaded models if PHOTOSORTER_TEST_MODELS is set, else skip."""
    d = os.environ.get("PHOTOSORTER_TEST_MODELS")
    if not d or not Path(d).is_dir():
        pytest.skip("set PHOTOSORTER_TEST_MODELS to a models folder to run model tests")
    old = os.environ.get("PHOTOSORTER_MODELS")
    os.environ["PHOTOSORTER_MODELS"] = d
    yield Path(d)
    if old is None:
        os.environ.pop("PHOTOSORTER_MODELS", None)
    else:
        os.environ["PHOTOSORTER_MODELS"] = old


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTOSORTER_HOME", str(tmp_path / "home"))
    if not os.environ.get("PHOTOSORTER_TEST_MODELS"):
        monkeypatch.setenv("PHOTOSORTER_MODELS", str(tmp_path / "no-models"))
