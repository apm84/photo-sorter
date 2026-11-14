"""Learn which duplicate you usually keep (v0.4). Local only; nothing leaves the PC.

How the suggestion worked before 0.4 (still the starting point): `dedupe.quality_key` ranks copies by
resolution, then an EXIF date, a camera model, GPS, file size, being the indexed copy of identical files, and
finally a shorter path. `pipeline.choose_keeper` never silently swaps a keeper you already have in the library
or approved; a better newcomer is only suggested ("needs review").

0.4 adds a pairwise preference model. Every single-group decision on the Clean up page where you keep some
copies and remove others is recorded as "kept X over Y" pairs, with each file's features frozen at that moment.
Keep all, Delete all and Approve all are *not* used (no preference in them). A logistic regression on feature
differences is fitted with a Gaussian prior centred on weights that reproduce the old ranking, so a handful of
decisions only nudges it; consistent choices over many groups move it. Once at least MIN_GROUPS decisions exist,
the model ranks the suggested keeper in pending groups (when it is fairly sure, P >= SWITCH_P) and in groups
found by later runs, with a short "why". Reset learning deletes the recorded decisions.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path

import numpy as np

FEATURES = [
    # name,            prior weight, phrase when the kept copy has MORE of it, phrase when LESS
    ("log_pixels",     2.0, "the higher-resolution copy", "the smaller-resolution copy"),
    ("log_size",       0.6, "the larger original file", "the smaller file"),
    ("exif_date",      1.0, "the copy with the original camera date", "the copy without a camera date"),
    ("camera",         0.8, "the copy with camera details", "the copy without camera details"),
    ("gps",            0.5, "the copy with a location", "the copy without a location"),
    ("jpeg",           0.0, "the JPEG", "the non-JPEG copy"),
    ("heic",           0.0, "the iPhone HEIC original", "the non-HEIC copy"),
    ("png",            0.0, "the PNG", "the non-PNG copy"),
    ("edited",         0.0, "the edited version", "the unedited original"),
    ("whatsapp",      -0.3, "the WhatsApp copy", "the copy that isn't from WhatsApp"),
    ("screenshot",    -0.2, "the screenshot", "the copy that isn't a screenshot"),
    ("copy_name",     -0.7, "the copy named like \"(1)\"/\"copy\"", "the copy without \"(1)\"/\"copy\" in its name"),
    ("sharpness",      0.0, "the sharper copy", "the softer copy"),
    ("date_later",     0.0, "the copy with the later date", "the copy with the earlier date"),
    ("path_len",      -0.3, "the copy in the longer folder path", "the copy in the shorter, original-looking folder"),
]
NAMES = [f[0] for f in FEATURES]
PRIOR = np.array([f[1] for f in FEATURES], dtype=np.float64)
# Gaussian prior precision per feature: higher = decisions move it less. The basic quality signals (resolution,
# file size, camera data) are firm defaults; the habit-like ones (edited, WhatsApp, "(1)" names, formats, folders)
# are what decisions are expected to teach, so they move easily. This keeps a confounded habit (the edited
# export was also bigger) from turning into "prefers low resolution" everywhere.
PRIOR_PRECISION = {"log_pixels": 8.0, "log_size": 4.0, "exif_date": 3.0, "camera": 3.0, "gps": 3.0,
                   "jpeg": 1.0, "heic": 1.0, "png": 1.0, "edited": 0.5, "whatsapp": 0.5, "screenshot": 0.5,
                   "copy_name": 0.5, "sharpness": 1.0, "date_later": 1.0, "path_len": 2.0}
PRIOR_STRENGTH = np.array([PRIOR_PRECISION[n] for n in NAMES], dtype=np.float64)
MIN_GROUPS = 3            # learning starts after this many recorded group decisions
SWITCH_P = 0.6            # pending groups switch their suggestion only when the model is at least this sure

_COPY_RE = re.compile(r"(\bcopy\b|\bkopie\b|\bcopie\b|\(\d+\)|~\d+$)", re.I)
_EDIT_RE = re.compile(r"(edit|edited|effects|snapseed|lightroom|retouch|filtered|export|_e$|-e$|\bfx\b)", re.I)
_EDIT_SOFTWARE = re.compile(r"(photoshop|lightroom|snapseed|instagram|vsco|picsart|facetune|gimp|canva)", re.I)
_WA_RE = re.compile(r"(whatsapp|IMG-\d{8}-WA\d+|WA\d{4})", re.I)
_SHOT_RE = re.compile(r"screenshot|screen shot|bildschirmfoto", re.I)


def _date_years(s: str | None) -> float | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s)[:19])
    except ValueError:
        return None
    return d.year + d.timetuple().tm_yday / 366.0


def sharpness(out: Path | None, fid) -> float | None:
    """Laplacian variance of the cached grid thumbnail (same size for every copy, so comparable). None if no
    thumbnail exists yet. Reads only the _index cache."""
    if out is None or fid is None:
        return None
    try:
        from . import thumbs
        from PIL import Image

        p = thumbs.cache_path(Path(out), int(fid), "thumb")
        if not p.exists():
            return None
        with Image.open(p) as im:
            g = np.asarray(im.convert("L"), dtype=np.float32)
        if g.shape[0] < 3 or g.shape[1] < 3:
            return None
        lap = (g[1:-1, :-2] + g[1:-1, 2:] + g[:-2, 1:-1] + g[2:, 1:-1] - 4 * g[1:-1, 1:-1])
        return float(lap.var())
    except Exception:
        return None


def features(f: dict, out: Path | None = None) -> dict:
    """{"v": [values], "k": [1 if known]} for one file row (dict with files columns)."""
    path = str(f.get("src_path") or "")
    name = str(f.get("filename") or Path(path).name)
    stem = Path(name).stem
    ext = (f.get("ext") or Path(name).suffix or "").lower().lstrip(".")
    w, h = f.get("width") or 0, f.get("height") or 0
    sh = f.get("_sharpness") if "_sharpness" in f else sharpness(out, f.get("id"))
    dy = _date_years(f.get("taken_at"))
    v = {
        "log_pixels": math.log2(w * h + 1) if w and h else 0.0,
        "log_size": math.log2((f.get("size") or 0) + 1),
        "exif_date": 1.0 if f.get("date_source") == "exif" else 0.0,
        "camera": 1.0 if (f.get("camera_make") or f.get("camera_model")) else 0.0,
        "gps": 1.0 if f.get("lat") is not None else 0.0,
        "jpeg": 1.0 if ext in ("jpg", "jpeg") else 0.0,
        "heic": 1.0 if ext in ("heic", "heif") else 0.0,
        "png": 1.0 if ext == "png" else 0.0,
        "edited": 1.0 if (_EDIT_RE.search(stem) or _EDIT_SOFTWARE.search(str(f.get("software") or ""))
                          or re.search(r"[\\/](edited|edits|exports?)[\\/]", path, re.I)) else 0.0,
        "whatsapp": 1.0 if _WA_RE.search(path) else 0.0,
        "screenshot": 1.0 if (_SHOT_RE.search(path) or f.get("type") == "screenshot") else 0.0,
        "copy_name": 1.0 if _COPY_RE.search(stem) else 0.0,
        "sharpness": math.log1p(sh) / 3.0 if sh is not None else 0.0,
        "date_later": dy / 10.0 if dy is not None else 0.0,
        "path_len": len(path) / 100.0,
    }
    known = {n: 1 for n in NAMES}
    known["log_pixels"] = 1 if (w and h) else 0
    known["sharpness"] = 1 if sh is not None else 0
    known["date_later"] = 1 if dy is not None else 0
    return {"v": [float(v[n]) for n in NAMES], "k": [known[n] for n in NAMES]}


def diff(fa: dict, fb: dict) -> np.ndarray:
    va, vb = np.array(fa["v"], np.float64), np.array(fb["v"], np.float64)
    known = np.array(fa["k"], np.float64) * np.array(fb["k"], np.float64)
    d = (va - vb) * known
    d[NAMES.index("date_later")] = float(np.clip(d[NAMES.index("date_later")], -0.3, 0.3))
    return d


# ---- storage ------------------------------------------------------------------------------------------------
def record_decision(db, action_id: int, group_id: int, members: list[dict], keep_ids, out: Path | None = None,
                    now: str | None = None) -> int:
    """Store "kept X over Y" pairs for one group decision. Ignores Keep all / Delete all (no preference)."""
    keep = {int(x) for x in keep_ids}
    kept = [m for m in members if m["id"] in keep]
    removed = [m for m in members if m["id"] not in keep]
    if not kept or not removed:
        return 0
    feats = {m["id"]: features(m, out) for m in members}
    w = 1.0 / (len(kept) * len(removed))       # one group = one unit of evidence, however big it is
    now = now or datetime.now().isoformat(timespec="seconds")
    rows = [(action_id, group_id, k["id"], r["id"], json.dumps(feats[k["id"]]), json.dumps(feats[r["id"]]), w, now)
            for k in kept for r in removed]
    db.executemany("INSERT INTO keep_decisions(action_id, group_id, kept_id, removed_id, kept_feat, removed_feat, "
                   "weight, created_at) VALUES(?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def forget_action(db, action_id: int) -> None:
    db.execute("DELETE FROM keep_decisions WHERE action_id=?", (action_id,))


def reset(db) -> int:
    n = db.one("SELECT COUNT(DISTINCT group_id || '-' || action_id) FROM keep_decisions")[0]
    db.execute("DELETE FROM keep_decisions")
    db.commit()
    db.set_meta("keep_learning_reset_at", datetime.now().isoformat(timespec="seconds"))
    return n


# ---- model -------------------------------------------------------------------------------------------------
def fit(D: np.ndarray, sw: np.ndarray, prior: np.ndarray = PRIOR, strength=PRIOR_STRENGTH,
        iters: int = 30) -> np.ndarray:
    """MAP logistic regression without intercept: P(kept > removed) = sigmoid(w . d), prior N(prior, 1/strength).
    Newton's method; a handful of dimensions, so this is instant."""
    w = prior.copy()
    if len(D) == 0:
        return w
    for _ in range(iters):
        z = D @ w
        p = 1.0 / (1.0 + np.exp(-z))
        g = -(D * (sw * (1 - p))[:, None]).sum(0) + strength * (w - prior)
        H = (D * (sw * p * (1 - p))[:, None]).T @ D + np.diag(np.broadcast_to(strength, w.shape))
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return w


class Model:
    def __init__(self, w: np.ndarray, groups: int, pairs: int):
        self.w, self.groups, self.pairs = w, groups, pairs

    @property
    def active(self) -> bool:
        return self.groups >= MIN_GROUPS

    def prob(self, fa: dict, fb: dict) -> float:
        return float(1.0 / (1.0 + math.exp(-float(diff(fa, fb) @ self.w))))

    def rank(self, members: list[dict], out: Path | None = None) -> tuple[int, dict, dict]:
        """(best id, {id: features}, {id: average P(beats the others)})."""
        feats = {m["id"]: features(m, out) for m in members}
        score = {}
        for m in members:
            others = [o for o in members if o["id"] != m["id"]]
            score[m["id"]] = (sum(self.prob(feats[m["id"]], feats[o["id"]]) for o in others) / len(others)
                              if others else 1.0)
        best = max(members, key=lambda m: (round(score[m["id"]], 9), -m["id"]))["id"]
        return best, feats, score

    def why(self, fa: dict, fb: dict) -> str | None:
        """Plain-language reason the model prefers a over b: the learned feature that helps a the most."""
        d = diff(fa, fb)
        contrib = d * self.w
        moved = np.abs(self.w - PRIOR) > 0.15          # prefer features the decisions actually taught
        order = sorted(range(len(NAMES)), key=lambda i: (not (moved[i] and contrib[i] > 0), -contrib[i]))
        i = order[0]
        if contrib[i] <= 0.05:
            return None
        return FEATURES[i][2] if d[i] > 0 else FEATURES[i][3]

    def top_lessons(self, n: int = 3) -> list[str]:
        """What the decisions taught, largest change from the starting weights first."""
        delta = self.w - PRIOR
        out = []
        for i in np.argsort(-np.abs(delta))[:n]:
            if abs(delta[i]) < 0.15:
                continue
            out.append(FEATURES[i][2] if delta[i] > 0 else FEATURES[i][3])
        return out


_cache: dict = {}


def load_model(db) -> Model:
    sig = db.one("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM keep_decisions")
    key = (str(getattr(db, "path", id(db))), sig[0], sig[1])
    if _cache.get("key") == key:
        return _cache["model"]
    rows = db.query("SELECT group_id, action_id, kept_feat, removed_feat, weight FROM keep_decisions")
    D = np.array([diff(json.loads(r["kept_feat"]), json.loads(r["removed_feat"])) for r in rows], np.float64) \
        if rows else np.zeros((0, len(NAMES)))
    sw = np.array([r["weight"] or 1.0 for r in rows], np.float64)
    groups = len({(r["group_id"], r["action_id"]) for r in rows})
    m = Model(fit(D, sw), groups, len(rows))
    _cache.update(key=key, model=m)
    return m


def status(db) -> dict:
    m = load_model(db)
    return {"decisions": m.groups, "pairs": m.pairs, "active": m.active, "min_decisions": MIN_GROUPS,
            "lessons": m.top_lessons() if m.active else [], "reset_at": db.get_meta("keep_learning_reset_at")}
