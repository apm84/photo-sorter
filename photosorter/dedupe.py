"""Duplicate grouping: exact (SHA-256) + near (pHash/dHash, confirmed by CLIP when available)."""
from __future__ import annotations

from collections import defaultdict

import numpy as np


class UnionFind:
    def __init__(self):
        self.p: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)

    def groups(self) -> list[list[int]]:
        g = defaultdict(list)
        for x in self.p:
            g[self.find(x)].append(x)
        return [sorted(v) for v in g.values() if len(v) > 1]


def _popcount(x: np.ndarray) -> np.ndarray:
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(x)
    # fallback for numpy<2
    x = x - ((x >> np.uint64(1)) & np.uint64(0x5555555555555555))
    x = (x & np.uint64(0x3333333333333333)) + ((x >> np.uint64(2)) & np.uint64(0x3333333333333333))
    x = (x + (x >> np.uint64(4))) & np.uint64(0x0F0F0F0F0F0F0F0F)
    return (x * np.uint64(0x0101010101010101)) >> np.uint64(56)


def near_pairs(ids: list[int], phash: list[int], dhash: list[int], max_p: int, max_d: int,
               chunk: int = 512) -> list[tuple[int, int, int]]:
    """All pairs (id_a, id_b, phash_distance) within thresholds. O(n^2) but vectorised (15k -> seconds)."""
    if len(ids) < 2:
        return []
    P = np.array([p & ((1 << 64) - 1) for p in phash], dtype=np.uint64)
    D = np.array([d & ((1 << 64) - 1) for d in dhash], dtype=np.uint64)
    out = []
    n = len(ids)
    for s in range(0, n, chunk):
        pa = P[s:s + chunk, None] ^ P[None, :]
        dp = _popcount(pa)
        da = _popcount(D[s:s + chunk, None] ^ D[None, :])
        rows, cols = np.nonzero((dp <= max_p) & (da <= max_d))
        for r, c in zip(rows.tolist(), cols.tolist()):
            i = s + r
            if c > i:
                out.append((ids[i], ids[c], int(dp[r, c])))
    return out


def quality_key(f: dict):
    """Higher is better: resolution, has EXIF date, has camera, file size, then shorter path."""
    return (
        (f.get("width") or 0) * (f.get("height") or 0),
        1 if f.get("date_source") == "exif" else 0,
        1 if (f.get("camera_make") or f.get("camera_model")) else 0,
        1 if f.get("lat") is not None else 0,
        f.get("size") or 0,
        1 if f.get("rep_id") == f.get("id") else 0,   # identical copies: keep the indexed representative
        -len(f.get("src_path") or ""),
    )


def pick_keeper(files: list[dict], model=None, out=None) -> int:
    """Best copy. With a learned preference model (v0.4, prefs.Model with enough decisions) the model ranks
    the copies; otherwise the fixed quality_key order."""
    if model is not None and getattr(model, "active", False) and len(files) > 1:
        return model.rank(files, out)[0]
    return max(files, key=quality_key)["id"]
