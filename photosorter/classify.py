"""Type classification: heuristics + optional image-AI zero-shot scores."""
from __future__ import annotations

import re

TYPES = ("photo", "screenshot", "meme", "document", "graphic")
UNSURE = 0.45  # below this AI confidence a non-photo guess falls back to "photo"

SCREEN_SIZES = {
    (640, 1136), (750, 1334), (828, 1792), (1080, 1920), (1125, 2436), (1170, 2532), (1179, 2556),
    (1242, 2208), (1242, 2688), (1284, 2778), (1290, 2796), (1080, 2340), (1080, 2400), (1080, 2280),
    (1440, 2560), (1440, 3040), (1440, 3200), (1440, 3088), (720, 1280), (720, 1600), (1536, 2048),
    (1668, 2388), (2048, 2732), (1620, 2160), (1640, 2360), (768, 1366), (900, 1440), (1080, 2160),
    (1200, 1920), (1050, 1680), (1600, 2560), (1800, 2880), (1440, 2304), (1964, 3024), (2234, 3456),
    (1600, 900), (1920, 1200), (2160, 3840), (1080, 2520), (1344, 2992), (1312, 2992), (1206, 2622), (1320, 2868),
}
SCREEN_SIZES |= {(b, a) for a, b in SCREEN_SIZES}

SCREENSHOT_NAME = re.compile(r"(screen ?shot|screenshot|scrnshot|screen_?capture|snip|capture\d|^scr_)", re.I)
MEME_NAME = re.compile(r"(meme|^fb_img_|^received_|^image_\d+|^tumblr_|^9gag|^ifunny|reddit)", re.I)
DOC_NAME = re.compile(r"(scan|receipt|invoice|document|^doc\d|statement)", re.I)


def heuristic_features(filename: str, width: int | None, height: int | None, fmt: str | None,
                       camera_make: str | None, camera_model: str | None, software: str | None = None) -> dict:
    w, h = width or 0, height or 0
    return {
        "has_camera": bool(camera_make or camera_model),
        "screen_dims": (w, h) in SCREEN_SIZES,
        "png": (fmt or "").upper() == "PNG",
        "screenshot_name": bool(SCREENSHOT_NAME.search(filename or "")),
        "meme_name": bool(MEME_NAME.search(filename or "")),
        "doc_name": bool(DOC_NAME.search(filename or "")),
        "small": max(w, h) < 900 if w and h else False,
    }


def classify(features: dict, ai_probs: dict[str, float] | None = None) -> tuple[str, str, dict]:
    """Return (type, source, scores). source in {'filename','heuristic','clip','ai+heuristic'}."""
    f = features
    if f["screenshot_name"] and not f["has_camera"]:
        return "screenshot", "filename", {"screenshot": 1.0}
    if ai_probs is None:
        if f["screen_dims"] and not f["has_camera"]:
            return "screenshot", "heuristic", {"screenshot": 0.8}
        if f["meme_name"] and not f["has_camera"]:
            return "meme", "heuristic", {"meme": 0.6}
        if f["doc_name"]:
            return "document", "heuristic", {"document": 0.6}
        if f["png"] and not f["has_camera"]:
            return "graphic", "heuristic", {"graphic": 0.5}
        return "photo", "heuristic", {"photo": 0.5}

    s = {k: float(ai_probs.get(k, 0.0)) for k in TYPES}
    if f["has_camera"]:
        # A real camera took it: almost always a photo (or a photo of a document).
        s["screenshot"] *= 0.05
        s["meme"] *= 0.1
        s["graphic"] *= 0.2
        s["photo"] += 0.2
    else:
        if f["screen_dims"]:
            s["screenshot"] += 0.45
        if f["png"]:
            s["screenshot"] += 0.1
            s["graphic"] += 0.05
            s["photo"] *= 0.6
        if f["meme_name"]:
            s["meme"] += 0.25
        if f["small"]:
            s["meme"] += 0.05
    if f["doc_name"]:
        s["document"] += 0.25
    total = sum(s.values()) or 1.0
    s = {k: v / total for k, v in s.items()}
    best = max(s, key=s.get)
    # Only move something out of Photos when the evidence is clear; unsure -> keep it with the photos.
    hinted = (best == "screenshot" and f["screen_dims"]) or (best == "meme" and f["meme_name"]) or \
             (best == "document" and f["doc_name"])
    if best != "photo" and s[best] < UNSURE and not hinted:
        best = "photo"
    return best, "ai+heuristic", {k: round(v, 4) for k, v in s.items()}
