"""Image decoding, EXIF, hashing helpers."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path

import imagehash
from PIL import Image, ImageOps

try:  # HEIC/HEIF support
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    try:  # v0.5 installer build: pi-heif, the decode-only edition (LGPL libheif + libde265, no x265 encoder)
        import pi_heif as pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:
        pillow_heif = None

Image.MAX_IMAGE_PIXELS = 400_000_000

EXIF_IFD = 0x8769
GPS_IFD = 0x8825
TAG_MAKE, TAG_MODEL, TAG_DATETIME = 0x010F, 0x0110, 0x0132
TAG_DT_ORIGINAL, TAG_DT_DIGITIZED = 0x9003, 0x9004
TAG_SOFTWARE = 0x0131


def sha256_file(path: Path, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(bufsize):
            h.update(chunk)
    return h.hexdigest()


def open_image(path: Path, max_side: int | None = None) -> Image.Image:
    """Open, honour EXIF orientation, convert to RGB. Uses JPEG draft mode to decode faster."""
    im = Image.open(path)
    if max_side and im.format == "JPEG":
        im.draft("RGB", (max_side, max_side))
    try:
        im.seek(0)
    except Exception:
        pass
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.BICUBIC)
    return im


def _ratio(v) -> float:
    try:
        return float(v)
    except Exception:
        try:
            return float(v[0]) / float(v[1])
        except Exception:
            return float("nan")


def _dms_to_deg(dms, ref) -> float | None:
    try:
        d, m, s = (_ratio(x) for x in dms)
        val = d + m / 60.0 + s / 3600.0
        if isinstance(ref, bytes):
            ref = ref.decode(errors="ignore")
        if str(ref).upper().strip("\x00 ") in ("S", "W"):
            val = -val
        if val != val:  # NaN
            return None
        return val
    except Exception:
        return None


def parse_exif_datetime(s) -> datetime | None:
    if not s:
        return None
    if isinstance(s, bytes):
        s = s.decode(errors="ignore")
    s = str(s).strip("\x00 ").strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s[:19], fmt)
            if 1900 < dt.year < 2100:
                return dt
        except ValueError:
            continue
    return None


_FN_PATTERNS = [
    # IMG_20190704_153012, PXL_20210101_..., Screenshot_20200101-120000, 20190704_153012
    re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})[_\-T ]?(\d{2})(\d{2})(\d{2})(?!\d)"),
    # 2019-07-04 15.30.12 / 2019-07-04_15-30-12 / Screen Shot 2019-07-04 at 3.30.12 PM
    re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.](\d{2})[-_.](\d{2})(?:[ _T\-]*(?:at )?(\d{1,2})[.\-:_](\d{2})[.\-:_](\d{2}))?"),
    # IMG-20190704-WA0001 (WhatsApp), 20190704
    re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)"),
]


def date_from_filename(name: str) -> datetime | None:
    for pat in _FN_PATTERNS:
        m = pat.search(name)
        if not m:
            continue
        g = [x for x in m.groups()]
        try:
            y, mo, d = int(g[0]), int(g[1]), int(g[2])
            hh = int(g[3]) if len(g) > 3 and g[3] else 0
            mi = int(g[4]) if len(g) > 4 and g[4] else 0
            ss = int(g[5]) if len(g) > 5 and g[5] else 0
            dt = datetime(y, mo, d, hh, mi, ss)
            if 1990 <= dt.year <= datetime.now().year + 1:
                return dt
        except ValueError:
            continue
    return None


def read_exif(im: Image.Image) -> dict:
    """Return {taken_at, camera_make, camera_model, software, lat, lon} (missing keys -> None)."""
    out = {"taken_at": None, "camera_make": None, "camera_model": None, "software": None, "lat": None, "lon": None}
    try:
        ex = im.getexif()
    except Exception:
        return out
    if not ex:
        return out

    def clean(v):
        if v is None:
            return None
        if isinstance(v, bytes):
            v = v.decode(errors="ignore")
        v = str(v).strip("\x00 ").strip()
        return v or None

    out["camera_make"] = clean(ex.get(TAG_MAKE))
    out["camera_model"] = clean(ex.get(TAG_MODEL))
    out["software"] = clean(ex.get(TAG_SOFTWARE))
    dt = None
    try:
        sub = ex.get_ifd(EXIF_IFD)
        dt = parse_exif_datetime(sub.get(TAG_DT_ORIGINAL)) or parse_exif_datetime(sub.get(TAG_DT_DIGITIZED))
    except Exception:
        pass
    dt = dt or parse_exif_datetime(ex.get(TAG_DATETIME))
    out["taken_at"] = dt
    try:
        gps = ex.get_ifd(GPS_IFD)
        if gps and 2 in gps and 4 in gps:
            lat = _dms_to_deg(gps[2], gps.get(1, "N"))
            lon = _dms_to_deg(gps[4], gps.get(3, "E"))
            if lat is not None and lon is not None and not (abs(lat) < 1e-6 and abs(lon) < 1e-6):
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    out["lat"], out["lon"] = lat, lon
    except Exception:
        pass
    return out


def perceptual_hashes(im: Image.Image) -> tuple[int, int]:
    ph = int(str(imagehash.phash(im)), 16)
    dh = int(str(imagehash.dhash(im)), 16)
    return ph, dh


def analyze_file(path: Path) -> dict:
    """Everything we can learn from one file without ML models."""
    path = Path(path)
    st = path.stat()
    res = {"size": st.st_size, "mtime": st.st_mtime, "sha256": sha256_file(path)}
    with Image.open(path) as raw:
        fmt = raw.format
        exif = read_exif(raw)
    im = open_image(path, max_side=512)
    # true dimensions (after orientation) from header, not the draft-decoded size
    with Image.open(path) as raw2:
        w, h = raw2.size
        try:
            orient = raw2.getexif().get(0x0112, 1)
        except Exception:
            orient = 1
    if orient in (5, 6, 7, 8):
        w, h = h, w
    ph, dh = perceptual_hashes(im)
    res.update({"width": w, "height": h, "format": fmt, "phash": ph, "dhash": dh, **exif})
    return res


# ---- unreadable files (v0.3) ------------------------------------------------------------------------------
HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
AVIF_BRANDS = {b"avif", b"avis"}
VIDEO_BRANDS = {b"qt  ", b"isom", b"iso2", b"mp41", b"mp42", b"m4v ", b"M4V ", b"3gp4", b"3gp5", b"3g2a", b"avc1",
                b"dash", b"MSNV", b"crx "}


def heic_support() -> str | None:
    """pillow-heif version string when HEIC/HEIF photos can be read, else None."""
    if pillow_heif is None:
        return None
    return getattr(pillow_heif, "__version__", "yes")


def sniff_kind(path: Path) -> str | None:
    """What a file really is, from its first bytes (ignores the extension)."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return None
    if not head:
        return "empty"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        brands = {brand} | {head[i:i + 4] for i in range(16, min(len(head), 48), 4)}
        if brands & HEIF_BRANDS:
            return "heif"
        if brands & AVIF_BRANDS:
            return "avif"
        if brand in VIDEO_BRANDS or brands & VIDEO_BRANDS:
            return "video"
        return "isobmff"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "video"
    if head[:4] in (b"\x1aE\xdf\xa3",):
        return "video"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    stripped = head.lstrip().lower()
    if stripped.startswith((b"<!doctype html", b"<html", b"<?xml", b"<svg")):
        return "html"
    if head.count(b"\x00") == len(head):
        return "zeros"
    return None


def unreadable_reason(path: Path, error: str | None) -> str:
    """Plain-English reason a file could not be read (for the log and _index/reports/unreadable.csv)."""
    err = (error or "").lower()
    kind = sniff_kind(Path(path))
    if "permission" in err or "access is denied" in err:
        return "no permission to read the file"
    if "no such file" in err or "cannot find" in err:
        return "file was moved or deleted during the scan"
    if kind == "empty":
        return "empty file (0 bytes)"
    if kind == "zeros":
        return "file is all zeros (damaged or incomplete copy)"
    if kind == "video":
        return "a video file with a photo extension (videos are not sorted)"
    if kind == "heif":
        if heic_support() is None:
            return "HEIC/HEIF photo, but HEIC support (pillow-heif) is not installed"
        return "HEIC/HEIF photo that pillow-heif could not decode (damaged, or an unsupported HEIC variant)"
    if kind == "avif":
        return "AVIF image (not supported by this Pillow version)"
    if kind == "html":
        return "a web page or text file saved with a photo extension"
    if "truncated" in err or "premature end" in err or "broken data stream" in err:
        return "file is cut off (incomplete copy or download)"
    if "decompressionbomb" in err or "exceeds limit" in err:
        return "image is too large to open safely"
    if "cannot identify image file" in err:
        return "not a recognised image format" + (f" (looks like {kind.upper()})" if kind else "")
    return "could not be decoded" + (f": {error[:120]}" if error else "")
