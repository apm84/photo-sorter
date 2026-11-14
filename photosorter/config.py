"""Paths and tunable settings."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp", ".tif", ".tiff"}

INDEX_DIRNAME = "_index"
DB_FILENAME = "photosorter.sqlite"


def app_data_dir() -> Path:
    """Per-user app folder (%LOCALAPPDATA%\\PhotoSorter on Windows)."""
    if os.environ.get("PHOTOSORTER_HOME"):
        base = Path(os.environ["PHOTOSORTER_HOME"])
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "PhotoSorter"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "photosorter"
    base.mkdir(parents=True, exist_ok=True)
    return base


def is_frozen() -> bool:
    """True when running from the installed PhotoSorter.exe (PyInstaller build)."""
    return bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    """Folder holding PhotoSorter.exe (installed) or the source checkout (run from source)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


APP_JSON = "app.json"   # v0.5: install-wide choices made in onboarding (models folder, update checks)


def load_app_config() -> dict:
    import json

    try:
        d = json.loads((app_data_dir() / APP_JSON).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_app_config(**changes) -> dict:
    import json

    d = load_app_config()
    d.update(changes)
    p = app_data_dir() / APP_JSON
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    tmp.replace(p)
    return d


def default_models_dir() -> Path:
    return app_data_dir() / "models"


def models_dir() -> Path:
    if os.environ.get("PHOTOSORTER_MODELS"):
        d = Path(os.environ["PHOTOSORTER_MODELS"])
    elif load_app_config().get("models_dir"):
        # v0.5: the folder picked in onboarding (models live outside the install folder, so updates keep them)
        d = Path(load_app_config()["models_dir"])
    else:
        # Source installs: prefer a "models" folder next to the app (e.g. D:\\PhotoSorter\\models).
        # Installed .exe: never inside the install folder (an upgrade would replace it), always app data.
        local = install_dir() / "models"
        d = local if (local.is_dir() and not is_frozen()) else default_models_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def index_dir(output_root: Path) -> Path:
    d = Path(output_root) / INDEX_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path(output_root: Path) -> Path:
    return index_dir(output_root) / DB_FILENAME


@dataclass
class Settings:
    sources: list[str] = field(default_factory=list)
    output: str = ""
    use_ai: bool = True                   # image AI: type sorting, tags, near-dupe check, text search
    embedder: str = "siglip2"             # "siglip2" (default, Apache-2.0) or "clip" (MIT)
    use_faces: bool = True
    face_pack: str = "yunet_sface"        # default (MIT/Apache); "buffalo_l"/"buffalo_s" = InsightFace, non-commercial
    use_gpu: bool = True                  # DirectML on Windows, CPU fallback
    mtime_date_fallback: bool = False     # use file modified time when no EXIF/filename date
    copy_exact_duplicates: bool = False   # exact (byte-identical) copies listed in a CSV instead of copied
    phash_max_distance: int = 6
    dhash_max_distance: int = 10
    ai_dupe_min_similarity: float = 0.92
    face_min_score: float = 0.6
    face_min_size: int = 40
    face_cluster_eps: float | None = None  # cosine distance cut; None = backend default for the method
    face_cluster_min_samples: int = 2
    face_cluster_method: str = "agglomerative"   # "agglomerative" (average linkage, default, any size) or "dbscan"
    # v0.3 face quality: faces below these are kept but marked low quality and skipped when grouping
    face_quality_min_px: int = 60          # face box short side, pixels at detection scale
    face_quality_min_score: float = 0.75   # detector confidence
    face_quality_min_blur: float = 35.0    # Laplacian variance of the 112x112 grey face crop
    workers: int = max(2, min(8, (os.cpu_count() or 4)))
    ai_batch: int = 16
    # v0.4 Run in background: "background" (below-normal priority, ~half the cores, waits while you use the PC),
    # "lowest" (idle priority) or "full" (every core, normal priority)
    run_mode: str = "background"
    idle_resume_s: int = 30               # background work resumes after this many seconds without input

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        if "face_quality_min_px" not in d:
            # settings saved by 0.1/0.2 stored the old default "dbscan": use the 0.3 default instead
            known.pop("face_cluster_method", None)
        return cls(**known)
