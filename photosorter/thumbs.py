"""Thumbnail and preview cache under <output>/_index/thumbs (v0.3).

Only this cache is ever written. Originals and the sorted copies are opened read-only and never modified,
resized or recompressed. Each image is generated once (WebP when Pillow supports it, else JPEG), EXIF-rotated,
and regenerated only when the photo itself changes (its file is newer than the cached image).
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import features

from .config import index_dir
from .imaging import open_image

THUMB_SIDE = 400        # long edge, grid thumbnails (sharp on 2x screens at ~170-200 px cards)
PREVIEW_SIDE = 1600     # long edge, the viewer
QUALITY = {"thumb": 80, "preview": 85}
WEBP = bool(features.check("webp"))
EXT = ".webp" if WEBP else ".jpg"
MEDIA_TYPE = "image/webp" if WEBP else "image/jpeg"


def cache_dir(out: Path) -> Path:
    d = index_dir(out) / "thumbs"
    d.mkdir(exist_ok=True)
    return d


def cache_path(out: Path, fid: int, kind: str = "thumb") -> Path:
    return cache_dir(out) / (f"{fid}{EXT}" if kind == "thumb" else f"{fid}_l{EXT}")


def source_path(out: Path, row) -> Path:
    """The library copy when there is one, else the original (both only read)."""
    if row["dest_path"]:
        p = Path(out) / row["dest_path"]
        if p.exists():
            return p
    return Path(row["src_path"])


def version(row) -> str:
    """Changes when the photo changes; used in thumbnail URLs so browsers can cache them for good."""
    return f"{int(row['mtime'] or 0)}-{row['size'] or 0}"


def ensure(out: Path, row, kind: str = "thumb") -> Path:
    """Path of the cached thumbnail/preview, generating it only if missing or older than the photo."""
    p = cache_path(out, row["id"], kind)
    src = source_path(out, row)
    try:
        if p.exists() and p.stat().st_mtime >= src.stat().st_mtime:
            return p
    except OSError:
        if p.exists():
            return p
    side = THUMB_SIDE if kind == "thumb" else PREVIEW_SIDE
    im = media_image(src, side, row)                       # EXIF-rotated photo, or a video's poster frame
    tmp = p.with_name(p.name + ".tmp")
    if WEBP:
        im.save(tmp, "WEBP", quality=QUALITY[kind], method=4)
    else:
        im.save(tmp, "JPEG", quality=QUALITY[kind], optimize=True)
    os.replace(tmp, p)
    legacy = cache_dir(out) / (f"{row['id']}.jpg" if kind == "thumb" else f"{row['id']}_l.jpg")
    if legacy != p:
        legacy.unlink(missing_ok=True)          # 0.2 cache file (320 px JPEG)
    return p


def _get(row, key, default=None):
    try:
        v = row[key]
        return default if v is None else v
    except (KeyError, IndexError, TypeError):
        return default


def media_image(src: Path, max_side: int | None, row=None, t: float | None = None):
    """RGB picture of a photo, or of a video frame (v0.6: posters at 10% in, at most 3 s; faces at their time)."""
    from . import video

    if video.is_video_path(src) or _get(row, "media") == "video":
        if t is None:
            t = video.poster_time(_get(row, "duration"))
        return video.frame(src, t, max_side, bool(_get(row, "hdr", False)))
    return open_image(src, max_side=max_side)


def etag(p: Path, kind: str) -> str:
    st = p.stat()
    return f'"{kind}-{p.stem}-{int(st.st_mtime)}-{st.st_size}"'
