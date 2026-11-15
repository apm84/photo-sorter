"""Videos (v0.6): reading, sampling and previewing videos with ffmpeg/ffprobe.

Everything runs locally. Originals and sorted copies are only ever read: ffmpeg is always given the video as
an input (-i) and writes only into <output>/_index (posters via the thumbnail cache, scrub sprites and
browser-playable proxies in _index/video). Nothing is uploaded anywhere.

ffmpeg/ffprobe come with the Windows installer (an LGPL build, next to PhotoSorter.exe in `ffmpeg\\`). From
source, set PHOTOSORTER_FFMPEG_DIR or have them on PATH. Without them videos are skipped (photos still work).

Work is light by design:
  * metadata from ffprobe (date, GPS, camera, length, codecs)
  * a handful of frames per video (`sample_times`, keyframe seeks), never a full decode, for faces, AI search
    and the duplicate signature
  * the scrub/hover sprite decodes keyframes only (-skip_frame nokey)
  * a 720p H.264 proxy only for videos a browser can't play as they are (e.g. iPhone HEVC .mov)
Child processes get no console window and inherit the low priority of Run in background (and are started
below normal priority explicitly on Windows); `-threads` follows the speed setting.
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from . import background
from .config import VIDEO_EXTS, index_dir, install_dir

log = logging.getLogger("photosorter")

PROXY_MAX_SIDE = 720            # short side of the proxy (720p)
PROXY_VIDEO_KBPS = 2500
SPRITE_TILE_W = 160
SPRITE_MAX = 100                # frames in the scrub sprite (10 x 10)
SPRITE_COLS = 10
SIG_POINTS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
H264_ENCODERS = ("h264_mf", "h264_amf", "h264_nvenc", "h264_qsv", "libopenh264", "libx264")
PLAYABLE_VCODECS = {"h264"}
PLAYABLE_ACODECS = {None, "aac", "mp3"}
HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}

CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
IDLE_PRIORITY_CLASS = 0x00000040


class VideoError(Exception):
    pass


def is_video_path(p) -> bool:
    return os.path.splitext(str(p))[1].lower() in VIDEO_EXTS


def is_video_row(row) -> bool:
    try:
        if row["media"] == "video":
            return True
    except (KeyError, IndexError, TypeError):
        pass
    try:
        return is_video_path(row["src_path"])
    except (KeyError, IndexError, TypeError):
        return False


# ---- finding ffmpeg ----------------------------------------------------------------------------------------
_tools: dict[str, str | None] = {}


def _candidate_dirs() -> list[Path]:
    dirs = []
    if os.environ.get("PHOTOSORTER_FFMPEG_DIR"):
        dirs.append(Path(os.environ["PHOTOSORTER_FFMPEG_DIR"]))
    dirs.append(install_dir() / "ffmpeg")
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        dirs.append(Path(mei) / "ffmpeg")
    dirs.append(install_dir() / "packaging" / "ffmpeg" / "bin")     # source checkout after tools/get_ffmpeg
    return dirs


def tool(name: str) -> str | None:
    """Full path of ffmpeg/ffprobe (bundled first, then PATH), or None."""
    if name in _tools:
        return _tools[name]
    if os.environ.get("PHOTOSORTER_FFMPEG_DIR") == "none":     # videos switched off (tests, troubleshooting)
        return None
    exe = name + (".exe" if sys.platform == "win32" else "")
    found = None
    for d in _candidate_dirs():
        if (d / exe).is_file():
            found = str(d / exe)
            break
    if found is None and os.environ.get("PHOTOSORTER_FFMPEG_DIR") != "none":
        found = shutil.which(name)
    _tools[name] = found
    return found


def reset_tools():
    _tools.clear()
    _encoder.clear()
    _filters.clear()


def available() -> bool:
    return bool(tool("ffmpeg") and tool("ffprobe"))


def version() -> str | None:
    if not tool("ffmpeg"):
        return None
    try:
        out = _run([tool("ffmpeg"), "-hide_banner", "-version"], timeout=20).stdout.decode("utf-8", "replace")
        return out.splitlines()[0].replace("ffmpeg version ", "").split(" Copyright")[0] if out else None
    except Exception:
        return None


def _popen_kw() -> dict:
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if sys.platform == "win32":
        mode = background.current_mode()
        flags = CREATE_NO_WINDOW
        if mode == "lowest":
            flags |= IDLE_PRIORITY_CLASS
        elif mode == "background":
            flags |= BELOW_NORMAL_PRIORITY_CLASS
        kw["creationflags"] = flags
    return kw


def _run(args: list[str], timeout: float = 120) -> subprocess.CompletedProcess:
    p = subprocess.Popen(args, **_popen_kw())
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise VideoError(f"ffmpeg took longer than {int(timeout)} s")
    return subprocess.CompletedProcess(args, p.returncode, out, err)


def _threads() -> list[str]:
    """-threads for ffmpeg: about half the cores in background modes, ffmpeg's own choice at full speed."""
    mode = background.current_mode()
    if mode == "full":
        return []
    return ["-threads", str(max(1, background.thread_budget(mode) // 2 or 1))]


def _ffmpeg(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    exe = tool("ffmpeg")
    if not exe:
        raise VideoError("ffmpeg not found")
    return _run([exe, "-hide_banner", "-loglevel", "error", "-nostdin", *args], timeout=timeout)


# ---- metadata ------------------------------------------------------------------------------------------------
_ISO6709 = re.compile(r"^\s*([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)")


def parse_iso6709(s) -> tuple[float, float] | None:
    """'+37.3349-122.0090+012.000/' -> (37.3349, -122.009). None for missing, invalid or 0,0."""
    m = _ISO6709.match(str(s or ""))
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (abs(lat) < 1e-6 and abs(lon) < 1e-6):
        return None
    return lat, lon


def parse_video_time(s) -> datetime | None:
    """Video creation time as local wall-clock time (naive, like EXIF dates).

    '2023-05-01T13:22:11-0500' (Apple creationdate, local with offset): the wall time as written.
    '2023-05-01T18:22:11.000000Z' (QuickTime/MP4 creation_time, UTC): converted to this PC's time zone.
    Zero/placeholder dates (1904, 1970) and dates in the future are ignored."""
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?$", s)
    if not m:
        return None
    try:
        dt = datetime(*(int(m.group(i)) for i in range(1, 7)))
    except ValueError:
        return None
    if dt.year < 1971:              # placeholders; also before converting (Windows can't for pre-1970 times)
        return None
    tz = m.group(7)
    if tz == "Z":
        dt = dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    if dt.year < 1971 or dt > datetime.now().replace(year=datetime.now().year + 1):
        return None
    return dt


def _rotation(vs: dict) -> int:
    rot = 0
    try:
        rot = int(float((vs.get("tags") or {}).get("rotate", 0)))
    except (TypeError, ValueError):
        pass
    for sd in vs.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rot = int(float(sd["rotation"]))
            except (TypeError, ValueError):
                pass
    return rot % 360


def probe(path) -> dict:
    """What ffprobe knows about a video. Raises VideoError if it isn't a readable video."""
    exe = tool("ffprobe")
    if not exe:
        raise VideoError("ffprobe not found")
    r = _run([exe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], timeout=60)
    if r.returncode != 0:
        raise VideoError((r.stderr or b"").decode("utf-8", "replace").strip()[:300] or "ffprobe failed")
    try:
        d = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as e:
        raise VideoError(f"ffprobe output unreadable: {e}")
    streams = d.get("streams") or []
    fmt = d.get("format") or {}
    vs = next((s for s in streams if s.get("codec_type") == "video"
               and not (s.get("disposition") or {}).get("attached_pic")), None)
    if vs is None:
        raise VideoError("no video stream")
    au = next((s for s in streams if s.get("codec_type") == "audio"), None)
    tags: dict = {}
    for src in (vs.get("tags") or {}, fmt.get("tags") or {}):
        for k, v in src.items():
            tags[str(k).lower()] = v
    dur = None
    for v in (fmt.get("duration"), vs.get("duration")):
        try:
            if v is not None and float(v) > 0:
                dur = float(v)
                break
        except (TypeError, ValueError):
            pass
    w, h = int(vs.get("width") or 0), int(vs.get("height") or 0)
    if _rotation(vs) in (90, 270):
        w, h = h, w
    taken = None
    for k in ("com.apple.quicktime.creationdate", "creation_time", "date"):
        taken = parse_video_time(tags.get(k))
        if taken:
            break
    loc = None
    for k in ("com.apple.quicktime.location.iso6709", "location", "location-eng"):
        loc = parse_iso6709(tags.get(k))
        if loc:
            break
    make = tags.get("com.apple.quicktime.make") or tags.get("com.android.manufacturer") or tags.get("make")
    model = tags.get("com.apple.quicktime.model") or tags.get("com.android.model") or tags.get("model")
    software = tags.get("com.apple.quicktime.software") or tags.get("com.android.version")
    info = {
        "duration": dur, "width": w or None, "height": h or None,
        "vcodec": (vs.get("codec_name") or "").lower() or None,
        "acodec": ((au or {}).get("codec_name") or "").lower() or None,
        "pix_fmt": vs.get("pix_fmt"), "format": fmt.get("format_name"),
        "hdr": (vs.get("color_transfer") or "") in HDR_TRANSFERS,
        "taken_at": taken, "lat": loc[0] if loc else None, "lon": loc[1] if loc else None,
        "camera_make": str(make)[:100] if make else None, "camera_model": str(model)[:100] if model else None,
        "software": str(software)[:100] if software else None,
    }
    info["playable"] = browser_playable(info)
    return info


def browser_playable(info: dict) -> bool:
    """True when Chrome/Edge/Firefox can play the file as it is (H.264 video, AAC/MP3/no audio, MP4/MOV, 8-bit)."""
    fmt = info.get("format") or ""
    return (info.get("vcodec") in PLAYABLE_VCODECS and info.get("acodec") in PLAYABLE_ACODECS
            and ("mp4" in fmt or "mov" in fmt) and (info.get("pix_fmt") or "yuv420p") in ("yuv420p", "yuvj420p")
            and not info.get("hdr"))


# ---- frames --------------------------------------------------------------------------------------------------
def sample_times(duration, n: int = 8) -> list[float]:
    """Up to n evenly spread moments (never the very first/last frame), about one per 2 s for short clips."""
    if not duration or duration <= 0:
        return [0.0]
    k = max(1, min(int(n), math.ceil(duration / 2.0)))
    return [round(duration * (i + 0.5) / k, 3) for i in range(k)]


def poster_time(duration) -> float:
    if not duration or duration <= 0:
        return 0.0
    return round(min(duration * 0.1, 3.0), 3)


_filters: dict[str, bool] = {}


def _has_filter(name: str) -> bool:
    if name not in _filters:
        try:
            out = _ffmpeg("-filters", timeout=20).stdout.decode("utf-8", "replace")
            _filters[name] = bool(re.search(rf"^\s*\S+\s+{re.escape(name)}\s", out, re.M))
        except Exception:
            _filters[name] = False
    return _filters[name]


def _tonemap(hdr: bool) -> str:
    """HDR (iPhone HLG/Dolby Vision) to SDR so pictures aren't washed out; needs zscale (in the bundled build)."""
    if hdr and _has_filter("zscale") and _has_filter("tonemap"):
        return ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
                "zscale=t=bt709:m=bt709:r=tv,")
    return ""


def frame(path, t: float = 0.0, max_side: int | None = None, hdr: bool = False) -> Image.Image:
    """One RGB frame near `t` seconds (the keyframe at or before it: fast, no full decode)."""
    vf = _tonemap(hdr)
    if max_side:
        vf += f"scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease,"
    vf += "format=rgb24"
    for at in ([t, 0.0] if t > 0 else [0.0]):
        r = _ffmpeg(*_threads(), "-ss", f"{at:.3f}", "-noaccurate_seek", "-i", str(path), "-frames:v", "1",
                    "-an", "-sn", "-vf", vf, "-f", "image2pipe", "-c:v", "bmp", "-", timeout=90)
        if r.returncode == 0 and r.stdout:
            im = Image.open(io.BytesIO(r.stdout))
            im.load()
            return im.convert("RGB")
    raise VideoError((r.stderr or b"").decode("utf-8", "replace").strip()[:300] or "no frame")


def frames(path, times, max_side: int | None = None, hdr: bool = False) -> list[tuple[float, Image.Image]]:
    out = []
    for t in times:
        try:
            out.append((t, frame(path, t, max_side, hdr)))
        except VideoError as e:
            log.debug("frame %.1fs of %s: %s", t, path, e)
    return out


def signature(path, duration, hdr: bool = False) -> list[int]:
    """dHashes of small frames at fixed positions: two copies of one video (re-encoded, resized) match."""
    from .imaging import perceptual_hashes
    from .db import to_signed64

    times = [duration * p for p in SIG_POINTS] if duration and duration > 0 else [0.0]
    return [to_signed64(perceptual_hashes(im)[1]) for _, im in frames(path, times, 160, hdr)]


def signature_distance(a: list[int], b: list[int]) -> float | None:
    """Median Hamming distance of aligned frame hashes (0 = same pictures), None if not comparable."""
    from .db import to_unsigned64

    n = min(len(a or []), len(b or []))
    if n == 0:
        return None
    d = sorted(bin(to_unsigned64(x) ^ to_unsigned64(y)).count("1") for x, y in zip(a[:n], b[:n]))
    return float(d[n // 2]) if n % 2 else (d[n // 2 - 1] + d[n // 2]) / 2.0


def same_video(a: dict, b: dict, max_dist: float = 10.0) -> bool:
    """Near-duplicate videos: about the same length and the same pictures at the same moments."""
    da, db_ = a.get("duration") or 0, b.get("duration") or 0
    if not da or not db_ or abs(da - db_) > max(1.0, 0.02 * max(da, db_)):
        return False
    d = signature_distance(a.get("sig") or [], b.get("sig") or [])
    return d is not None and d <= max_dist


def analyze_video(path) -> dict:
    """Same keys as imaging.analyze_file, plus the video fields. Reads the file only."""
    from .imaging import sha256_file

    path = Path(path)
    st = path.stat()
    info = probe(path)
    res = {"size": st.st_size, "mtime": st.st_mtime, "sha256": sha256_file(path), "format": "VIDEO",
           "phash": None, "dhash": None, "media": "video", **info}
    try:
        res["sig"] = signature(path, info["duration"], info["hdr"])
    except VideoError:
        res["sig"] = []
    return res


# ---- caches in _index/video ------------------------------------------------------------------------------------
def cache_dir(out) -> Path:
    d = index_dir(Path(out)) / "video"
    d.mkdir(exist_ok=True)
    return d


def sprite_path(out, fid: int) -> Path:
    return cache_dir(out) / f"{fid}.sprite.jpg"


def proxy_path(out, fid: int) -> Path:
    return cache_dir(out) / f"{fid}.proxy.mp4"


def sprite_layout(duration) -> tuple[int, float]:
    """(frames, seconds per frame): about one per second, at most SPRITE_MAX."""
    if not duration or duration <= 0:
        return 1, 1.0
    n = max(1, min(SPRITE_MAX, int(math.ceil(duration))))
    return n, duration / n


def make_sprite(path, duration, dest: Path, hdr: bool = False) -> dict:
    """Scrub/hover sprite sheet from keyframes only (fast). Returns its layout."""
    n, step = sprite_layout(duration)
    rows = max(1, math.ceil(n / SPRITE_COLS))
    cols = min(SPRITE_COLS, n)
    tmp = dest.with_name(dest.name + f".{os.getpid()}.tmp.jpg")
    vf = f"{_tonemap(hdr)}fps={1.0 / step:.6f},scale={SPRITE_TILE_W}:-2,tile={cols}x{rows}"
    r = _ffmpeg(*_threads(), "-skip_frame", "nokey", "-i", str(path), "-an", "-sn", "-vf", vf, "-frames:v", "1",
                "-q:v", "5", "-y", str(tmp), timeout=max(120, (duration or 0) * 2))
    if r.returncode != 0 or not tmp.exists():
        # some files have too few keyframes for -skip_frame: decode everything instead (slower, still capped)
        r = _ffmpeg(*_threads(), "-i", str(path), "-an", "-sn", "-vf", vf, "-frames:v", "1", "-q:v", "5", "-y",
                    str(tmp), timeout=max(300, (duration or 0) * 6))
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise VideoError((r.stderr or b"").decode("utf-8", "replace").strip()[:300] or "sprite failed")
    with Image.open(tmp) as im:
        W, H = im.size
    os.replace(tmp, dest)
    return {"n": n, "cols": cols, "interval": round(step, 4), "tw": W // cols, "th": H // rows}


_encoder: dict[str, str | None] = {}


def h264_encoder() -> str | None:
    """First H.264 encoder that really works here (Windows Media Foundation, AMD/NVIDIA/Intel GPUs, OpenH264)."""
    if "h264" in _encoder:
        return _encoder["h264"]
    found = None
    if os.environ.get("PHOTOSORTER_H264_ENCODER"):
        found = os.environ["PHOTOSORTER_H264_ENCODER"]
    elif tool("ffmpeg"):
        try:
            listed = _ffmpeg("-encoders", timeout=20).stdout.decode("utf-8", "replace")
        except Exception:
            listed = ""
        for enc in H264_ENCODERS:
            if not re.search(rf"^\s*V\S*\s+{enc}\s", listed, re.M):
                continue
            try:
                r = _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=0.5", "-pix_fmt", "yuv420p",
                            "-c:v", enc, "-f", "null", "-", timeout=30)
            except VideoError:
                continue
            if r.returncode == 0:
                found = enc
                break
    _encoder["h264"] = found
    return found


def _encoder_args(enc: str) -> list[str]:
    if enc == "libx264":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    if enc == "h264_mf":
        return ["-c:v", "h264_mf", "-b:v", f"{PROXY_VIDEO_KBPS}k", "-rate_control", "quality"]
    return ["-c:v", enc, "-b:v", f"{PROXY_VIDEO_KBPS}k", "-maxrate", f"{PROXY_VIDEO_KBPS * 2}k",
            "-bufsize", f"{PROXY_VIDEO_KBPS * 2}k"]


def make_proxy(path, dest: Path, duration=None, hdr: bool = False, has_audio: bool = True) -> str:
    """720p H.264/AAC MP4 (fast start) that every browser plays. Returns the encoder used."""
    enc = h264_encoder()
    if not enc:
        raise VideoError("no H.264 encoder available in this ffmpeg")
    s = PROXY_MAX_SIDE
    vf = (f"{_tonemap(hdr)}scale='if(gt(iw,ih),-2,min({s},iw))':'if(gt(iw,ih),min({s},ih),-2)',"
          "format=yuv420p")
    tmp = dest.with_name(dest.name + f".{os.getpid()}.partial.mp4")
    args = [*_threads(), "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-vf", vf,
            *_encoder_args(enc), "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart",
            "-map_metadata", "-1", "-y", str(tmp)]
    r = _ffmpeg(*args, timeout=max(600, (duration or 60) * 20))
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise VideoError((r.stderr or b"").decode("utf-8", "replace").strip()[-300:] or "proxy failed")
    os.replace(tmp, dest)
    return enc


def vtt(meta: dict, sprite_url: str, duration) -> str:
    """WebVTT thumbnails track for the sprite sheet (sprite.jpg#xywh=...)."""
    def ts(x):
        x = max(0.0, x)
        return f"{int(x // 3600):02d}:{int(x % 3600 // 60):02d}:{x % 60:06.3f}"
    lines = ["WEBVTT", ""]
    for i in range(meta["n"]):
        a = i * meta["interval"]
        b = min(duration or (a + meta["interval"]), a + meta["interval"])
        x, y = (i % meta["cols"]) * meta["tw"], (i // meta["cols"]) * meta["th"]
        lines += [f"{ts(a)} --> {ts(b)}", f"{sprite_url}#xywh={x},{y},{meta['tw']},{meta['th']}", ""]
    return "\n".join(lines)


# ---- index helpers (shared by the pipeline and the web app) --------------------------------------------------------
def best_source(out, row) -> Path:
    if row["dest_path"]:
        p = Path(out) / row["dest_path"]
        if p.exists():
            return p
    return Path(row["src_path"])


def ensure_sprite(db, out, fid: int) -> dict | None:
    """Sprite layout for a video, making the sprite if missing. None if it can't be made."""
    r = db.one("SELECT f.id, f.src_path, f.dest_path, f.duration, v.hdr, v.sprite_n, v.sprite_cols, "
               "v.sprite_interval, v.tile_w, v.tile_h FROM files f LEFT JOIN videos v ON v.file_id=f.id "
               "WHERE f.id=? AND f.media='video'", (fid,))
    if not r:
        return None
    p = sprite_path(out, fid)
    if r["sprite_n"] and p.exists():
        return {"n": r["sprite_n"], "cols": r["sprite_cols"], "interval": r["sprite_interval"],
                "tw": r["tile_w"], "th": r["tile_h"]}
    try:
        meta = make_sprite(best_source(out, r), r["duration"], p, bool(r["hdr"]))
    except (VideoError, OSError) as e:
        log.warning("Video preview failed for %s: %s", r["src_path"], e)
        return None
    db.execute("INSERT OR IGNORE INTO videos(file_id) VALUES(?)", (fid,))
    db.execute("UPDATE videos SET sprite_n=?, sprite_cols=?, sprite_interval=?, tile_w=?, tile_h=? WHERE file_id=?",
               (meta["n"], meta["cols"], meta["interval"], meta["tw"], meta["th"], fid))
    db.commit()
    return meta


def proxy_state(db, out, fid: int) -> str:
    """'not_needed' (plays as is), 'ok' (proxy ready), 'failed', 'missing' (not made yet)."""
    r = db.one("SELECT f.playable, v.proxy FROM files f LEFT JOIN videos v ON v.file_id=f.id WHERE f.id=?", (fid,))
    if not r:
        return "missing"
    if r["playable"]:
        return "not_needed"
    if r["proxy"] == "ok" and proxy_path(out, fid).exists():
        return "ok"
    if r["proxy"] == "failed":
        return "failed"
    return "missing"


def ensure_proxy(db, out, fid: int) -> str:
    st = proxy_state(db, out, fid)
    if st in ("not_needed", "ok", "failed"):
        return st
    r = db.one("SELECT f.src_path, f.dest_path, f.duration, f.acodec, v.hdr FROM files f "
               "LEFT JOIN videos v ON v.file_id=f.id WHERE f.id=?", (fid,))
    db.execute("INSERT OR IGNORE INTO videos(file_id) VALUES(?)", (fid,))
    try:
        enc = make_proxy(best_source(out, r), proxy_path(out, fid), r["duration"], bool(r["hdr"]), bool(r["acodec"]))
        db.execute("UPDATE videos SET proxy='ok', proxy_error=NULL WHERE file_id=?", (fid,))
        log.info("Playable copy made with %s: %s", enc, r["src_path"])
        st = "ok"
    except (VideoError, OSError) as e:
        log.warning("Playable copy failed for %s: %s", r["src_path"], e)
        db.execute("UPDATE videos SET proxy='failed', proxy_error=? WHERE file_id=?", (str(e)[:300], fid))
        st = "failed"
    db.commit()
    return st


class Jobs:
    """One low-priority background thread in the web app for on-demand sprites and proxies."""

    def __init__(self, db, out):
        self.db, self.out = db, Path(out)
        self.q: queue.Queue = queue.Queue()
        self.pending: set = set()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def submit(self, kind: str, fid: int) -> bool:
        key = (kind, int(fid))
        with self.lock:
            if key in self.pending:
                return False
            self.pending.add(key)
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="video-jobs", daemon=True)
                self.thread.start()
        self.q.put(key)
        return True

    def busy(self, kind: str, fid: int) -> bool:
        with self.lock:
            return (kind, int(fid)) in self.pending

    def _loop(self):
        background.lower_thread_priority(background.saved_mode())
        while True:
            try:
                key = self.q.get(timeout=60)
            except queue.Empty:
                with self.lock:
                    if self.q.empty():
                        self.thread = None
                        return
                continue
            kind, fid = key
            try:
                if kind == "proxy":
                    ensure_proxy(self.db, self.out, fid)
                elif kind == "sprite":
                    ensure_sprite(self.db, self.out, fid)
            except Exception:
                log.exception("video job %s %s failed", kind, fid)
            finally:
                with self.lock:
                    self.pending.discard(key)


def wait_idle(jobs: Jobs, timeout: float = 30) -> bool:
    """Tests: wait until the job queue is empty."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        with jobs.lock:
            if not jobs.pending:
                return True
        time.sleep(0.05)
    return False
