"""Face grouping and learning from the user's corrections (v0.3).

- Grouping: average-linkage agglomerative clustering (default) on cosine distance, computed exactly by the
  nearest-neighbour-chain algorithm with O(n*d) memory (no n x n matrix, so it works far past 8,000 faces),
  or DBSCAN (optional). There is no silent fallback from one to the other.
- Quality: faces that are small, blurry or low-confidence are kept but marked ``low_quality`` and left out of
  grouping. They can still join a named person, but only on a very confident match.
- Learning: each named/kept person has several reference faces (their confirmed faces first) and negative
  examples (faces the user said are not them). New faces join the person with the nearest reference only with
  a clear margin over the second-best person and never when they are close to one of that person's negatives.
  Borderline faces go to an "Is this <Name>?" queue (faces.suggest_person).
- What the user did is never undone: names, merges/assignments (locked faces), removals (negatives) survive.
"""
from __future__ import annotations

import logging
from datetime import datetime

import numpy as np

from .db import DB, blob_to_vec, vec_to_blob

log = logging.getLogger("photosorter")

METHODS = ("agglomerative", "dbscan")
REF_CAP = 40           # reference faces kept per person
NEG_CAP = 100          # negative examples kept per person
MARGIN = 0.05          # best person must beat the second-best by this much (cosine similarity)
BORDER_BAND = 0.10     # below the match threshold by up to this much -> "Is this <Name>?" queue
NEG_NEAR = 0.85        # this similar to a rejected face counts as "close to a negative" outright
STRICT_EXTRA = 0.10    # low-quality faces need this much more similarity (and margin) to join a person
DEFAULT_QUALITY = {"min_px": 60.0, "min_score": 0.75, "min_blur": 35.0}
DETECT_MAX_SIDE = 1600  # photos are decoded at this size for face detection (pipeline.stage_faces)


# ---- average-linkage clustering ----------------------------------------------------------------------------
def upgma_labels(X: np.ndarray, threshold: float, sizes=None) -> np.ndarray:
    """Exact average-linkage (UPGMA) clusters on cosine distance, merging while the distance is < threshold.

    Rows of X are unit vectors (or, with ``sizes``, sums of that many unit vectors). The average pairwise cosine
    similarity of two groups is dot(sum_a, sum_b) / (n_a * n_b), so a group is fully described by its sum and
    size. The nearest-neighbour chain finds the same merges as the textbook algorithm (average linkage is
    reducible) in O(n^2 * d) time and O(n * d) memory. Same result as sklearn's AgglomerativeClustering(
    metric="cosine", linkage="average", distance_threshold=threshold), without its n x n distance matrix."""
    n = len(X)
    if n == 0:
        return np.zeros(0, np.int64)
    S = np.asarray(X, dtype=np.float64).copy()
    cnt = np.ones(n) if sizes is None else np.asarray(sizes, dtype=np.float64).copy()
    S32 = S.astype(np.float32)
    avail = np.ones(n, bool)
    parent = np.arange(n)
    chain: list[int] = []
    nxt = 0
    while True:
        if not chain:
            while nxt < n and not avail[nxt]:
                nxt += 1
            if nxt >= n:
                break
            chain.append(nxt)
        a = chain[-1]
        sims = (S32 @ S32[a]).astype(np.float64) / (cnt * cnt[a])
        sims[~avail] = -np.inf
        sims[a] = -np.inf
        b = int(np.argmax(sims))
        if len(chain) >= 2 and sims[chain[-2]] >= sims[b]:
            b = chain[-2]            # ties go back down the chain, so it always terminates
        if not np.isfinite(sims[b]) or 1.0 - sims[b] >= threshold:
            # a's nearest group is too far, and (reducibility) any later merge is at least as far: a is final
            avail[a] = False
            chain.pop()
            continue
        if len(chain) >= 2 and b == chain[-2]:
            chain.pop(); chain.pop()
            S[b] += S[a]; cnt[b] += cnt[a]; S32[b] = S[b]
            avail[a] = False
            parent[a] = b
        else:
            chain.append(b)
    root = parent.copy()
    for i in range(n):
        r = i
        while root[r] != r:
            r = root[r]
        root[i] = r
    return np.unique(root, return_inverse=True)[1].astype(np.int64)


def cluster_labels(X: np.ndarray, eps: float, min_samples: int, method: str) -> np.ndarray:
    """Cluster label per row (-1 = not in any group). No fallback between methods."""
    if method not in METHODS:
        raise ValueError(f"unknown face grouping method {method!r}")
    if len(X) == 0:
        return np.zeros(0, np.int64)
    if method == "dbscan":
        from sklearn.cluster import DBSCAN

        return DBSCAN(eps=eps, min_samples=min_samples, metric="cosine", n_jobs=-1).fit_predict(X)
    labels = upgma_labels(X, eps)
    counts = np.bincount(labels)
    return np.where(counts[labels] >= max(1, min_samples), labels, -1)


# ---- quality ---------------------------------------------------------------------------------------------
def blur_score(bgr_or_rgb: np.ndarray, box) -> float:
    """Laplacian variance of the face crop resized to 112x112 grey (sharp faces: hundreds; blurry: < ~35)."""
    import cv2

    h, w = bgr_or_rgb.shape[:2]
    x1, y1, x2, y2 = [int(round(v)) for v in box]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 - x1 < 4 or y2 - y1 < 4:
        return 0.0
    crop = bgr_or_rgb[y1:y2, x1:x2]
    grey = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY) if crop.ndim == 3 else crop
    grey = cv2.resize(grey, (112, 112), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(grey, cv2.CV_64F).var())


def quality_thresholds(db: DB) -> dict:
    s = db.get_meta("settings") or {}
    q = dict(DEFAULT_QUALITY)
    for key, name in (("min_px", "face_quality_min_px"), ("min_score", "face_quality_min_score"),
                      ("min_blur", "face_quality_min_blur")):
        if s.get(name) is not None:
            q[key] = float(s[name])
    q.update({k: float(v) for k, v in (db.get_meta("face_quality") or {}).items() if k in q})
    return q


def backfill_face_px(db: DB) -> int:
    """Face size for faces found before 0.3 (from the stored box and the photo size at detection scale)."""
    rows = db.query("SELECT fa.id, fa.x1, fa.y1, fa.x2, fa.y2, f.width, f.height FROM faces fa "
                    "JOIN files f ON f.id=fa.file_id WHERE fa.face_px IS NULL AND f.width>0 AND f.height>0")
    upd = []
    for r in rows:
        W, H = r["width"], r["height"]
        sc = min(1.0, DETECT_MAX_SIDE / max(W, H))
        upd.append((min((r["x2"] - r["x1"]) * W, (r["y2"] - r["y1"]) * H) * sc, r["id"]))
    db.executemany("UPDATE faces SET face_px=? WHERE id=?", upd)
    db.commit()
    return len(upd)


def mark_low_quality(db: DB, q: dict | None = None) -> int:
    """Set faces.low_quality from the stored measurements (unknown measurements never count against a face)."""
    q = q or quality_thresholds(db)
    db.execute("UPDATE faces SET low_quality = CASE WHEN (face_px IS NOT NULL AND face_px < ?) "
               "OR (score IS NOT NULL AND score < ?) OR (blur IS NOT NULL AND blur >= 0 AND blur < ?) THEN 1 ELSE 0 END",
               (q["min_px"], q["min_score"], q["min_blur"]))
    db.commit()
    return db.one("SELECT COUNT(*) FROM faces WHERE low_quality=1")[0]


# ---- references and negatives -----------------------------------------------------------------------------
def _unit(X: np.ndarray) -> np.ndarray:
    X = np.asarray(X, dtype=np.float32)
    if X.ndim == 1:
        return X / max(float(np.linalg.norm(X)), 1e-8)
    return X / np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-8)


def _fps(E: np.ndarray, k: int, start: list[int] | None = None) -> list[int]:
    """Farthest-point sampling: k varied rows (different ages, angles, glasses...)."""
    n = len(E)
    if n <= k:
        return list(range(n))
    chosen = list(start or [])
    if not chosen:
        c = _unit(E.mean(axis=0))
        chosen = [int(np.argmax(E @ c))]
    mind = np.min(1.0 - E @ E[chosen].T, axis=1)
    while len(chosen) < k:
        i = int(np.argmax(mind))
        if mind[i] <= 0:
            break
        chosen.append(i)
        mind = np.minimum(mind, 1.0 - E @ E[i])
    return chosen


def select_refs(E: np.ndarray, confirmed: np.ndarray, cap: int = REF_CAP) -> np.ndarray:
    """Reference faces for one person: confirmed (user-assigned / merged / 'Yes') faces first, then the rest of
    the group, as a varied subset. Faces far from the group's centre are left out of the unconfirmed part."""
    E = _unit(E)
    confirmed = np.asarray(confirmed, bool)
    ci = np.where(confirmed)[0]
    ui = np.where(~confirmed)[0]
    pick = [int(ci[j]) for j in _fps(E[ci], cap)] if len(ci) else []
    if len(pick) < cap and len(ui):
        c = _unit(E.mean(axis=0))
        sims = E[ui] @ c
        if len(ui) >= 10:   # drop the least typical 10% of auto-assigned faces (likely mistakes)
            ui = ui[sims >= np.quantile(sims, 0.10)]
        sub = E[ui]
        base = E[pick] if pick else None
        extra = []
        if base is not None:
            mind = np.min(1.0 - sub @ base.T, axis=1)
        else:
            first = int(np.argmax(sub @ c))
            extra = [first]
            mind = 1.0 - sub @ sub[first]
        while len(pick) + len(extra) < cap and len(extra) < len(ui):
            i = int(np.argmax(mind))
            if mind[i] < 0 or i in extra:
                break
            extra.append(i)
            mind = np.minimum(mind, 1.0 - sub @ sub[i])
            mind[i] = -1
        pick += [int(ui[i]) for i in extra]
    return E[pick] if pick else np.zeros((0, E.shape[1]), np.float32)


def person_examples(db: DB, people_ids) -> tuple[dict, dict]:
    """(refs, negs): person id -> unit vectors. Computed from the faces table (always current)."""
    refs, negs = {}, {}
    for pid in people_ids:
        rows = db.query("SELECT embedding, locked FROM faces WHERE person_id=? AND embedding IS NOT NULL "
                        "AND (low_quality=0 OR locked=1)", (pid,))
        if rows:
            E = np.stack([blob_to_vec(r[0]) for r in rows])
            refs[pid] = select_refs(E, np.array([bool(r[1]) for r in rows]))
        nrows = db.query("SELECT fa.embedding FROM face_negatives n JOIN faces fa ON fa.id=n.face_id "
                         "WHERE n.person_id=? AND fa.embedding IS NOT NULL ORDER BY n.rowid DESC LIMIT ?",
                         (pid, NEG_CAP))
        if nrows:
            negs[pid] = _unit(np.stack([blob_to_vec(r[0]) for r in nrows]))
    return refs, negs


def banned_people(db: DB) -> dict[int, set[int]]:
    out: dict[int, set[int]] = {}
    for r in db.query("SELECT face_id, person_id FROM face_negatives"):
        out.setdefault(r[0], set()).add(r[1])
    for r in db.query("SELECT id, not_person FROM faces WHERE not_person IS NOT NULL"):
        out.setdefault(r[0], set()).add(r[1])
    return out


class Matcher:
    """Nearest-reference matching with a margin and negative examples."""

    def __init__(self, refs: dict, negs: dict, match_sim: float, margin: float = MARGIN):
        self.pids = [p for p in refs if len(refs[p])]
        self.refs, self.negs = refs, negs
        self.match_sim, self.margin = match_sim, margin

    def scores(self, X: np.ndarray) -> np.ndarray:
        """n x P: similarity of each face to each person's nearest reference."""
        X = _unit(np.atleast_2d(X))
        if not self.pids:
            return np.zeros((len(X), 0), np.float32)
        return np.stack([(X @ self.refs[p].T).max(axis=1) for p in self.pids], axis=1)

    def neg_sim(self, x: np.ndarray, pid: int) -> float:
        n = self.negs.get(pid)
        return float((n @ _unit(x)).max()) if n is not None and len(n) else -1.0

    def neg_close(self, x: np.ndarray, pid: int, pos_sim: float | None = None) -> bool:
        """Close to one of the person's negatives: about as close as to the person themselves, or nearly the
        same face as one the user rejected."""
        ns = self.neg_sim(x, pid)
        if ns < 0:
            return False
        if pos_sim is None:
            r = self.refs.get(pid)
            pos_sim = float((r @ _unit(x)).max()) if r is not None and len(r) else -1.0
        return ns >= NEG_NEAR or (ns >= pos_sim - 0.02 and ns >= self.match_sim - BORDER_BAND)

    def ranked(self, x: np.ndarray, banned=()) -> list[tuple[int, float]]:
        s = self.scores(x)[0] if self.pids else []
        out = [(p, float(v)) for p, v in zip(self.pids, s) if p not in banned]
        return sorted(out, key=lambda t: -t[1])

    def decide(self, x: np.ndarray, banned=(), strict: bool = False):
        """(person or None, (best person, similarity) or None for the borderline queue)."""
        r = self.ranked(x, banned)
        if not r:
            return None, None
        (bp, bs) = r[0]
        second = r[1][1] if len(r) > 1 else -1.0
        need = self.match_sim + (STRICT_EXTRA if strict else 0.0)
        margin = self.margin + (STRICT_EXTRA / 2 if strict else 0.0)
        if bs >= need and bs - second >= margin and not self.neg_close(x, bp, bs):
            return bp, None
        if bs >= self.match_sim - BORDER_BAND:
            return None, (bp, bs)
        return None, None

    def match_group(self, Xc: np.ndarray, banned_by_face: list[set]) -> int | None:
        """A whole new cluster joins a person when its faces are, on average, close to that person's references
        with a clear margin, and most of them are not close to the person's negatives."""
        if not self.pids:
            return None
        S = self.scores(Xc)
        mean = S.mean(axis=0)
        order = np.argsort(-mean)
        bp = self.pids[int(order[0])]
        bs = float(mean[order[0]])
        second = float(mean[order[1]]) if len(order) > 1 else -1.0
        if bs < self.match_sim or bs - second < self.margin:
            return None
        col = S[:, int(order[0])]
        bad = sum(1 for x, b, ps in zip(Xc, banned_by_face, col) if bp in b or self.neg_close(x, bp, float(ps)))
        return bp if bad * 2 < len(Xc) else None


# ---- grouping ---------------------------------------------------------------------------------------------
def kept_people_ids(db: DB) -> set[int]:
    return {r[0] for r in db.query(
        "SELECT id FROM people WHERE (name IS NOT NULL AND name<>'') OR hidden=1 "
        "OR id IN (SELECT person_id FROM faces WHERE locked=1 AND person_id IS NOT NULL)")}


def cluster_faces(db: DB, eps: float, min_samples: int, method: str = "agglomerative") -> dict:
    """Group face embeddings into people. Returns stats. What the user did is never undone by re-grouping:
    named/hidden people keep their names, locked faces (merged, assigned, 'Yes') stay put, and a face is never
    put into a person it was removed from (negatives)."""
    if method not in METHODS:
        raise ValueError(f"unknown face grouping method {method!r}")
    backfill_face_px(db)
    nlow = mark_low_quality(db)
    rows = db.query("SELECT id, embedding, person_id, locked, low_quality FROM faces WHERE embedding IS NOT NULL")
    stats = {"faces": len(rows), "low_quality": nlow, "method": method, "eps": eps}
    if not rows:
        return stats
    kept = kept_people_ids(db)
    refs, negs = person_examples(db, kept)
    banned = banned_people(db)
    match_sim = max(0.4, 1.0 - eps)          # cosine similarity that counts as "same person"
    m = Matcher(refs, negs, match_sim)
    free = [r for r in rows if not r["locked"]]
    good = [r for r in free if not r["low_quality"]]
    low = [r for r in free if r["low_quality"]]
    vec = lambda rs: _unit(np.stack([blob_to_vec(r["embedding"]) for r in rs])) if rs else np.zeros((0, 1), np.float32)
    X, XL = vec(good), vec(low)
    labels = cluster_labels(X, eps, min_samples, method)
    stats["grouped"] = len(good)

    db.execute("UPDATE faces SET person_id=NULL WHERE locked=0 AND person_id IS NOT NULL AND person_id NOT IN "
               f"({','.join(map(str, kept)) or 'NULL'})")
    if kept:
        db.execute(f"DELETE FROM people WHERE id NOT IN ({','.join(map(str, kept))})")
    else:
        db.execute("DELETE FROM people")
    prev = {r["id"]: (r["person_id"] if r["person_id"] in kept else None) for r in free}
    assign: dict[int, int | None] = {}
    suggest: dict[int, tuple[int, float]] = {}

    def single(fid, x, strict=False, prev_pid=None):
        b = banned.get(fid, set())
        if prev_pid is not None and prev_pid not in b and not strict:
            assign[fid] = prev_pid          # already in this person: stays (only new joins are checked)
            return
        pid, sug = m.decide(x, b, strict=strict)
        assign[fid] = pid
        if sug:
            suggest[fid] = sug

    for lab in sorted(set(labels.tolist())):
        idx = np.where(labels == lab)[0]
        if lab == -1:
            for i in idx:
                single(good[i]["id"], X[i], prev_pid=prev[good[i]["id"]])
            continue
        votes = [prev[good[i]["id"]] for i in idx if prev[good[i]["id"]]]
        pid = max(set(votes), key=votes.count) if votes else None
        if pid is None:
            pid = m.match_group(X[idx], [banned.get(good[i]["id"], set()) for i in idx])
        if pid is None:
            c = _unit(X[idx].mean(axis=0))
            pid = db.execute("INSERT INTO people(name, centroid) VALUES(NULL, ?)", (vec_to_blob(c),)).lastrowid
        for i in idx:
            fid = good[i]["id"]
            if pid in banned.get(fid, set()):
                single(fid, X[i])          # not this person: maybe someone else, else the borderline queue
            elif prev[fid] != pid and pid in kept:
                # joining a named/kept person: nearest reference, clear margin, not close to a negative
                single(fid, X[i])
            else:
                assign[fid] = pid
    for i, r in enumerate(low):             # low quality: only a very confident match to a named/kept person
        single(r["id"], XL[i], strict=True)

    # borderline queue for faces that are not in any person, including removed (locked) ones
    for r in db.query("SELECT id, embedding FROM faces WHERE locked=1 AND person_id IS NULL AND embedding IS NOT NULL"):
        _, sug = m.decide(blob_to_vec(r["embedding"]), banned.get(r["id"], set()))
        if sug:
            suggest[r["id"]] = sug
    db.executemany("UPDATE faces SET person_id=? WHERE id=?", [(p, f) for f, p in assign.items()])
    db.execute("UPDATE faces SET suggest_person=NULL, suggest_sim=NULL")
    db.executemany("UPDATE faces SET suggest_person=?, suggest_sim=? WHERE id=? AND person_id IS NULL",
                   [(p, round(s, 4), f) for f, (p, s) in suggest.items()])
    refresh_people(db)
    stats["people"] = db.one("SELECT COUNT(*) FROM people")[0]
    stats["borderline"] = db.one("SELECT COUNT(*) FROM faces WHERE suggest_person IS NOT NULL")[0]
    log.info("Face grouping: %s (strictness %.2f), %d faces, %d low quality skipped, %d groups, %d to confirm",
             method, eps, len(rows), len(low), stats["people"], stats["borderline"])
    return stats


def refresh_people(db: DB):
    """Recompute each person's centroid, face count, reference faces and negatives; drop empty unnamed people."""
    kept = kept_people_ids(db)
    for p in db.query("SELECT id FROM people"):
        pid = p["id"]
        rows = db.query("SELECT embedding, locked, low_quality FROM faces WHERE person_id=? AND embedding IS NOT NULL",
                        (pid,))
        if not rows:
            db.execute("DELETE FROM people WHERE id=? AND (name IS NULL OR name='') AND hidden=0", (pid,))
            db.execute("UPDATE people SET face_count=0, centroid=NULL, refs=NULL, ref_count=0 WHERE id=?", (pid,))
            continue
        E = np.stack([blob_to_vec(r[0]) for r in rows])
        c = _unit(E.mean(axis=0))
        refs_blob, nref, negs_blob = None, 0, None
        if pid in kept:
            ok = np.array([bool(r[1]) or not r[2] for r in rows])
            if ok.any():
                R = select_refs(E[ok], np.array([bool(r[1]) for r in rows])[ok])
                refs_blob, nref = vec_to_blob(R.reshape(-1)), len(R)
            nrows = db.query("SELECT fa.embedding FROM face_negatives n JOIN faces fa ON fa.id=n.face_id "
                             "WHERE n.person_id=? AND fa.embedding IS NOT NULL ORDER BY n.rowid DESC LIMIT ?",
                             (pid, NEG_CAP))
            if nrows:
                negs_blob = vec_to_blob(np.stack([blob_to_vec(r[0]) for r in nrows]).reshape(-1))
        db.execute("UPDATE people SET centroid=?, face_count=?, refs=?, ref_count=?, negs=? WHERE id=?",
                   (vec_to_blob(c), len(rows), refs_blob, nref, negs_blob, pid))
    db.commit()


def add_negative(db: DB, face_id: int, person_id: int):
    db.execute("INSERT OR IGNORE INTO face_negatives(face_id, person_id, created_at) VALUES(?,?,?)",
               (face_id, person_id, datetime.now().isoformat(timespec="seconds")))


def face_cluster_params(db: DB, s=None) -> tuple[float, int, str]:
    """eps / min_samples / method: web-app choice (meta) > Settings > defaults (agglomerative, 0.45 for SFace)."""
    from .faces import FACE_BACKENDS

    saved = db.get_meta("face_cluster") or {}
    method = saved.get("method") or (getattr(s, "face_cluster_method", None) if s else None) or "agglomerative"
    if method not in METHODS:
        method = "agglomerative"
    backend = FACE_BACKENDS.get(db.get_meta("face_backend") or (s.face_pack if s else "yunet_sface"))
    default_eps = (getattr(backend, "agglo_eps", 0.45) if method == "agglomerative"
                   else getattr(backend, "cluster_eps", 0.55)) if backend else (0.45 if method == "agglomerative" else 0.55)
    eps = saved.get("eps") or (s.face_cluster_eps if s else None) or default_eps
    ms = int(saved.get("min_samples") or (s.face_cluster_min_samples if s else 2))
    return float(eps), ms, method
