"""Model registry, one-time download, and ONNX Runtime sessions (DirectML -> CPU fallback)."""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__
from .config import models_dir

log = logging.getLogger("photosorter")

HF = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main"
IF_REL = "https://github.com/deepinsight/insightface/releases/download/v0.7"
ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"
# v0.5: the self-exported SigLIP 2 ONNX files (and a mirror of the OpenCV Zoo face models) are assets of the
# "models-v1" release of this repo. Every file is checked against the SHA-256 below after download.
REPO = "apm84/photo-sorter"
MODELS_RELEASE_URL = f"https://github.com/{REPO}/releases/tag/models-v1"
SIGLIP_BASE = (os.environ.get("PHOTOSORTER_MODEL_BASE_URL", "").rstrip("/")
               or f"https://github.com/{REPO}/releases/download/models-v1")


@dataclass
class ModelFile:
    key: str
    url: str
    dest: str                 # relative to models dir
    approx_mb: int
    license: str
    extract: tuple[str, ...] = ()   # for zip downloads: member basenames to keep
    sha256: str = ""                # expected SHA-256 of the downloaded file ("" = not pinned)
    size: int = 0                   # exact size in bytes when known
    mirrors: tuple[str, ...] = ()   # other URLs for the same bytes (tried in order after `url`)


SIGLIP_LICENSE = "Apache-2.0 (google/siglip2-base-patch16-224; ONNX self-exported by tools/export_siglip2.py)"

MODELS: dict[str, list[ModelFile]] = {
    # --- image/text embedders (types, tags, near-duplicate check, text search) ---
    "siglip2": [
        ModelFile("siglip2_vision", f"{SIGLIP_BASE}/siglip2_vision.onnx", "siglip2/vision_model.onnx", 372, SIGLIP_LICENSE,
                  sha256="6f7aae3358312653fb77a4b384366a0f5776e8e27e20a72a5f6069069d75995c", size=371815190),
        ModelFile("siglip2_text", f"{SIGLIP_BASE}/siglip2_text_int8.onnx", "siglip2/text_model_int8.onnx", 540, SIGLIP_LICENSE,
                  sha256="9ddf58b5fa761154e348af2185987d07c5e2dbacf84c3901f9de73b1976491ee", size=539581152),
        ModelFile("siglip2_tokenizer", f"{SIGLIP_BASE}/siglip2_tokenizer.json", "siglip2/tokenizer.json", 34, SIGLIP_LICENSE,
                  sha256="caefd63119539a63be2d55ef3e05023fbb793948c4bda5bc0c366b42a382f903", size=34363038),
        ModelFile("siglip2_config", f"{SIGLIP_BASE}/siglip2_config.json", "siglip2/config.json", 1, SIGLIP_LICENSE,
                  sha256="cb3aef39675691616db442cdbb2ea7610175f8a2892caad5061604ff7956426c", size=202),
    ],
    "clip": [  # optional alternative embedder
        ModelFile("clip_vision", f"{HF}/onnx/vision_model.onnx", "clip/vision_model.onnx", 352,
                  "MIT (OpenAI CLIP ViT-B/32); ONNX export by Xenova on Hugging Face"),
        ModelFile("clip_text", f"{HF}/onnx/text_model_quantized.onnx", "clip/text_model_quantized.onnx", 65,
                  "MIT (OpenAI CLIP ViT-B/32); ONNX export by Xenova on Hugging Face"),
        ModelFile("clip_tokenizer", f"{HF}/tokenizer.json", "clip/tokenizer.json", 3, "MIT"),
    ],
    # --- face backends ---
    "yunet_sface": [  # default: official OpenCV Zoo files, mirrored byte for byte in the models-v1 release
        ModelFile("yunet", f"{ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
                  "faces/yunet_sface/face_detection_yunet_2023mar.onnx", 1, "MIT (OpenCV Zoo YuNet)",
                  sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4", size=232589,
                  mirrors=(f"{SIGLIP_BASE}/face_detection_yunet_2023mar.onnx",)),
        ModelFile("sface", f"{ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
                  "faces/yunet_sface/face_recognition_sface_2021dec.onnx", 37, "Apache-2.0 (OpenCV Zoo SFace)",
                  sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79", size=38696353,
                  mirrors=(f"{SIGLIP_BASE}/face_recognition_sface_2021dec.onnx",)),
    ],
    "buffalo_l": [  # optional upgrade, user-downloaded only, never bundled
        ModelFile("faces_l", f"{IF_REL}/buffalo_l.zip", "faces/buffalo_l", 289,
                  "InsightFace pretrained weights: NON-COMMERCIAL RESEARCH USE ONLY",
                  extract=("det_10g.onnx", "w600k_r50.onnx")),
    ],
    "buffalo_s": [
        ModelFile("faces_s", f"{IF_REL}/buffalo_s.zip", "faces/buffalo_s", 128,
                  "InsightFace pretrained weights: NON-COMMERCIAL RESEARCH USE ONLY",
                  extract=("det_500m.onnx", "w600k_mbf.onnx")),
    ],
}

FACE_FILES = {"buffalo_l": ("det_10g.onnx", "w600k_r50.onnx"), "buffalo_s": ("det_500m.onnx", "w600k_mbf.onnx"),
              "yunet_sface": ("face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx")}
NON_COMMERCIAL = {"buffalo_l", "buffalo_s"}


class DownloadCancelled(Exception):
    pass


class ChecksumError(RuntimeError):
    pass


def model_path(rel: str) -> Path:
    return models_dir() / rel


def is_installed(group: str) -> bool:
    for mf in MODELS[group]:
        p = model_path(mf.dest)
        if mf.extract:
            if not all((p / name).exists() for name in mf.extract):
                return False
        elif not p.exists():
            return False
    return True


def group_bytes(group: str) -> int:
    """Approximate download size of a model group."""
    return sum(mf.size or mf.approx_mb << 20 for mf in MODELS[group])


def missing_bytes(group: str) -> int:
    tot = 0
    for mf in MODELS[group]:
        p = model_path(mf.dest)
        have = all((p / n).exists() for n in mf.extract) if mf.extract else p.exists()
        if not have:
            tot += mf.size or mf.approx_mb << 20
    return tot


def sha256_file(path: Path, chunk: int = 1 << 20, cancel: Callable[[], bool] | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            if cancel and cancel():
                raise DownloadCancelled()
            h.update(b)
    return h.hexdigest()


def download_file(urls, dest: Path, progress: Callable[[str, int, int], None] | None = None, label: str = "",
                  sha256: str = "", size: int = 0, retries: int = 3, cancel: Callable[[], bool] | None = None,
                  timeout: float = 60, sleep: Callable[[float], None] = time.sleep) -> Path:
    """Download to `dest` via `dest.part`: resumes a partial file (HTTP Range), retries with backoff and the
    next mirror, and checks the SHA-256 before the file is moved into place. Nothing is ever uploaded."""
    urls = [urls] if isinstance(urls, str) else [u for u in urls if u]
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    last: Exception | None = None
    for attempt in range(max(1, retries) * len(urls)):
        url = urls[attempt % len(urls)]
        try:
            have = tmp.stat().st_size if tmp.exists() else 0
            if size and have > size:
                tmp.unlink(); have = 0
            if not (size and have == size):
                headers = {"User-Agent": f"PhotoSorter/{__version__}"}
                if have:
                    headers["Range"] = f"bytes={have}-"
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    status = getattr(r, "status", 200)
                    if have and status != 206:        # server ignored the range: start again
                        have = 0
                    total = int(r.headers.get("Content-Length") or 0) + have
                    total = size or total
                    with open(tmp, "ab" if have else "wb") as f:
                        done = have
                        while True:
                            if cancel and cancel():
                                raise DownloadCancelled()
                            chunk = r.read(1 << 20)
                            if not chunk:
                                break
                            f.write(chunk)
                            done += len(chunk)
                            if progress:
                                progress(label, done, total)
            if size and tmp.stat().st_size != size:
                raise IOError(f"incomplete download ({tmp.stat().st_size} of {size} bytes)")
            if sha256:
                got = sha256_file(tmp, cancel=cancel)
                if got.lower() != sha256.lower():
                    tmp.unlink(missing_ok=True)
                    raise ChecksumError(f"{label or dest.name}: checksum mismatch (got {got[:12]}...)")
            tmp.replace(dest)
            return dest
        except DownloadCancelled:
            raise
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 416:                         # range past the end: the .part is bad
                tmp.unlink(missing_ok=True)
        except Exception as e:                        # network drop, timeout, checksum: retry
            last = e
        log.warning("Download of %s failed (attempt %d): %s", label or url, attempt + 1, last)
        if attempt + 1 < max(1, retries) * len(urls):
            sleep(min(30, 2 ** (attempt // len(urls))))
    raise RuntimeError(f"Could not download {label or dest.name}: {last}")


def _download(url: str, dest: Path, progress: Callable[[str, int, int], None] | None, label: str):
    download_file(url, dest, progress, label)


def ensure_models(group: str, progress: Callable[[str, int, int], None] | None = None,
                  cancel: Callable[[], bool] | None = None) -> None:
    """Download a model group once into the local cache (resumable, SHA-256 checked)."""
    for mf in MODELS[group]:
        p = model_path(mf.dest)
        if mf.extract:
            if all((p / n).exists() for n in mf.extract):
                continue
            zpath = p.with_suffix(".zip")
            log.info("Downloading %s (~%d MB) - license: %s", mf.key, mf.approx_mb, mf.license)
            download_file((mf.url,) + mf.mirrors, zpath, progress, mf.key, mf.sha256, mf.size, cancel=cancel)
            p.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zpath) as z:
                for member in z.namelist():
                    base = Path(member).name
                    if base in mf.extract:
                        with z.open(member) as src, open(p / base, "wb") as dst:
                            shutil.copyfileobj(src, dst)
            zpath.unlink(missing_ok=True)
        elif not p.exists():
            if mf.url.startswith("/"):
                raise RuntimeError(f"No download location for {group} models. Run Export-Models.bat once "
                                   "(or set PHOTOSORTER_MODEL_BASE_URL).")
            log.info("Downloading %s (~%d MB) - license: %s", mf.key, mf.approx_mb, mf.license)
            download_file((mf.url,) + mf.mirrors, p, progress, mf.key, mf.sha256, mf.size, cancel=cancel)


def verify_group(group: str, progress: Callable[[str, int, int], None] | None = None) -> list[str]:
    """Names of installed files whose checksum does not match (empty list = all good or not pinned)."""
    bad = []
    for mf in MODELS[group]:
        p = model_path(mf.dest)
        if mf.sha256 and p.is_file():
            if progress:
                progress(mf.key, 0, mf.size)
            if (mf.size and p.stat().st_size != mf.size) or sha256_file(p) != mf.sha256:
                bad.append(mf.dest)
    return bad


def import_models_from(folder: Path, groups=("siglip2", "yunet_sface"),
                       progress: Callable[[str, int, int], None] | None = None) -> list[str]:
    """Copy already-downloaded model files from another models folder (e.g. a source install's `models`
    folder) after checking each file's SHA-256. Returns the files copied."""
    folder = Path(folder)
    copied = []
    for g in groups:
        for mf in MODELS[g]:
            if mf.extract or not mf.sha256:
                continue
            src, dst = folder / mf.dest, model_path(mf.dest)
            if dst.exists() or not src.is_file() or (mf.size and src.stat().st_size != mf.size):
                continue
            if sha256_file(src) != mf.sha256:
                continue
            if progress:
                progress(mf.key, 0, mf.size)
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".part")
            shutil.copyfile(src, tmp)
            tmp.replace(dst)
            copied.append(mf.dest)
    return copied


def available_providers() -> list[str]:
    import onnxruntime as ort

    return ort.get_available_providers()


def make_session(path: Path, use_gpu: bool = True, gpu_ok: bool = True):
    """Create an ONNX Runtime session. Prefers DirectML (AMD/Intel/NVIDIA on Windows), falls back to CPU."""
    import onnxruntime as ort

    from .background import ort_threads

    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    if ort_threads():                          # v0.4 Run in background: leave cores for the user
        so.intra_op_num_threads = ort_threads()
        so.inter_op_num_threads = 1
    avail = ort.get_available_providers()
    if use_gpu and gpu_ok and "DmlExecutionProvider" in avail:
        so.enable_mem_pattern = False          # required by DirectML
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        try:
            s = ort.InferenceSession(str(path), sess_options=so,
                                     providers=["DmlExecutionProvider", "CPUExecutionProvider"])
            log.info("%s: using GPU (DirectML)", Path(path).name)
            return s
        except Exception as e:  # pragma: no cover - Windows only
            log.warning("DirectML failed for %s (%s); using CPU", Path(path).name, e)
            so = ort.SessionOptions()
            if ort_threads():
                so.intra_op_num_threads = ort_threads()
                so.inter_op_num_threads = 1
    s = ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])
    log.info("%s: using CPU", Path(path).name)
    return s


class ResilientSession:
    """Wraps a session; if a GPU run raises, rebuilds on CPU and retries once."""

    def __init__(self, path: Path, use_gpu: bool = True):
        self.path = Path(path)
        self.sess = make_session(self.path, use_gpu)
        self.on_gpu = "DmlExecutionProvider" in self.sess.get_providers()

    def run(self, feeds: dict):
        try:
            return self.sess.run(None, feeds)
        except Exception as e:
            if not self.on_gpu:
                raise
            log.warning("GPU inference failed (%s); switching %s to CPU", e, self.path.name)
            self.sess = make_session(self.path, use_gpu=False)
            self.on_gpu = False
            return self.sess.run(None, feeds)

    def input_name(self) -> str:
        return self.sess.get_inputs()[0].name
