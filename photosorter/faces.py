"""Pluggable face backends.

- yunet_sface (default): OpenCV Zoo YuNet detector (MIT) + SFace recognizer (Apache-2.0), run by OpenCV DNN (CPU).
- buffalo_l / buffalo_s (optional upgrade): InsightFace SCRFD + ArcFace via ONNX Runtime (DirectML/CPU).
  Their pretrained weights are licensed for NON-COMMERCIAL research use only; user-downloaded, never bundled.
"""
from __future__ import annotations

import cv2
import numpy as np

from .models import FACE_FILES, ResilientSession, model_path

ARCFACE_DST = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                        [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)


def _nms(dets: np.ndarray, thresh: float) -> list[int]:
    x1, y1, x2, y2, scores = dets.T
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[order[1:]]); yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]]); yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1 + 1); h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[np.where(ovr <= thresh)[0] + 1]
    return keep


class SCRFD:
    def __init__(self, path, use_gpu=True, input_size=640):
        self.sess = ResilientSession(path, use_gpu)
        self.input_name = self.sess.input_name()
        self.size = input_size
        n_out = len(self.sess.sess.get_outputs())
        self.fmc, self.strides, self.num_anchors = 3, [8, 16, 32], 2
        if n_out not in (9, 6):
            raise ValueError(f"Unexpected SCRFD outputs: {n_out}")
        self.use_kps = n_out == 9
        self._centers: dict = {}

    def detect(self, bgr: np.ndarray, thresh=0.5, nms_thresh=0.4):
        h0, w0 = bgr.shape[:2]
        S = self.size
        if h0 / w0 > 1:
            nh, nw = S, int(S * w0 / h0)
        else:
            nw, nh = S, int(S * h0 / w0)
        scale = nh / h0
        canvas = np.zeros((S, S, 3), dtype=np.uint8)
        canvas[:nh, :nw] = cv2.resize(bgr, (nw, nh))
        blob = cv2.dnn.blobFromImage(canvas, 1.0 / 128, (S, S), (127.5, 127.5, 127.5), swapRB=True)
        outs = self.sess.run({self.input_name: blob})
        scores_l, boxes_l, kps_l = [], [], []
        for i, stride in enumerate(self.strides):
            scores = outs[i].reshape(-1)
            bbox = outs[i + self.fmc].reshape(-1, 4) * stride
            fh, fw = S // stride, S // stride
            key = (fh, fw, stride)
            if key not in self._centers:
                c = np.stack(np.mgrid[:fh, :fw][::-1], axis=-1).astype(np.float32)
                c = (c * stride).reshape(-1, 2)
                c = np.stack([c] * self.num_anchors, axis=1).reshape(-1, 2)
                self._centers[key] = c
            c = self._centers[key]
            pos = np.where(scores >= thresh)[0]
            b = np.stack([c[:, 0] - bbox[:, 0], c[:, 1] - bbox[:, 1], c[:, 0] + bbox[:, 2], c[:, 1] + bbox[:, 3]], -1)
            scores_l.append(scores[pos]); boxes_l.append(b[pos])
            if self.use_kps:
                k = outs[i + 2 * self.fmc].reshape(-1, 10) * stride
                kp = np.empty_like(k)
                kp[:, 0::2] = c[:, :1] + k[:, 0::2]
                kp[:, 1::2] = c[:, 1:2] + k[:, 1::2]
                kps_l.append(kp[pos])
        scores = np.concatenate(scores_l)
        if scores.size == 0:
            return np.zeros((0, 5), np.float32), np.zeros((0, 5, 2), np.float32)
        boxes = np.concatenate(boxes_l) / scale
        kps = (np.concatenate(kps_l) / scale).reshape(-1, 5, 2) if self.use_kps else None
        dets = np.hstack([boxes, scores[:, None]]).astype(np.float32)
        keep = _nms(dets, nms_thresh)
        return dets[keep], (kps[keep] if kps is not None else None)


class ArcFace:
    def __init__(self, path, use_gpu=True):
        self.sess = ResilientSession(path, use_gpu)
        self.input_name = self.sess.input_name()

    @staticmethod
    def align(bgr: np.ndarray, kps: np.ndarray) -> np.ndarray:
        M, _ = cv2.estimateAffinePartial2D(kps.astype(np.float32), ARCFACE_DST, method=cv2.LMEDS)
        return cv2.warpAffine(bgr, M, (112, 112), borderValue=0.0)

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        blob = cv2.dnn.blobFromImages(crops, 1.0 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True)
        out = []
        for i in range(blob.shape[0]):  # model has batch dim 1 in some exports
            out.append(self.sess.run({self.input_name: blob[i:i + 1]})[0][0])
        e = np.stack(out).astype(np.float32)
        return e / np.maximum(np.linalg.norm(e, axis=1, keepdims=True), 1e-8)


class InsightFaceEngine:
    name = "insightface"
    cluster_eps = 0.5   # cosine distance for DBSCAN (ArcFace)
    agglo_eps = 0.55    # average-linkage cut (agglomerative, the 0.3 default)

    def __init__(self, pack: str = "buffalo_l", use_gpu: bool = True):
        det, rec = FACE_FILES[pack]
        base = model_path(f"faces/{pack}")
        self.det = SCRFD(base / det, use_gpu)
        self.rec = ArcFace(base / rec, use_gpu)

    def faces(self, rgb_image, min_score=0.6, min_size=40) -> list[dict]:
        """rgb PIL image -> [{bbox, score, embedding}] in the image's pixel coordinates."""
        bgr = cv2.cvtColor(np.asarray(rgb_image), cv2.COLOR_RGB2BGR)
        dets, kps = self.det.detect(bgr, thresh=min_score)
        found, crops = [], []
        for i, d in enumerate(dets):
            x1, y1, x2, y2, s = d.tolist()
            if min(x2 - x1, y2 - y1) < min_size or kps is None:
                continue
            crops.append(ArcFace.align(bgr, kps[i]))
            found.append({"bbox": (x1, y1, x2, y2), "score": s})
        if crops:
            embs = self.rec.embed(crops)
            for f, e in zip(found, embs):
                f["embedding"] = e
        return found


class YuNetSFaceEngine:
    """OpenCV YuNet + SFace. 128-d embeddings; runs on CPU through OpenCV (fast enough: ~20-60 ms/photo)."""
    name = "yunet_sface"
    cluster_eps = 0.55  # cosine distance for DBSCAN (SFace same-person similarity is lower than ArcFace)
    agglo_eps = 0.45    # average-linkage cut; 0.45 worked far better than DBSCAN on a real ~4,000-face library

    def __init__(self, pack: str = "yunet_sface", use_gpu: bool = True):
        det, rec = FACE_FILES["yunet_sface"]
        base = model_path("faces/yunet_sface")
        self.det = cv2.FaceDetectorYN.create(str(base / det), "", (320, 320), 0.6, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(str(base / rec), "")

    def faces(self, rgb_image, min_score=0.6, min_size=40) -> list[dict]:
        bgr = cv2.cvtColor(np.asarray(rgb_image), cv2.COLOR_RGB2BGR)
        h, w = bgr.shape[:2]
        self.det.setInputSize((w, h))
        self.det.setScoreThreshold(min_score)
        _, dets = self.det.detect(bgr)
        found = []
        if dets is None:
            return found
        for d in dets:
            x, y, bw, bh = d[:4].tolist()
            if min(bw, bh) < min_size:
                continue
            aligned = self.rec.alignCrop(bgr, d)
            e = self.rec.feature(aligned).reshape(-1).astype(np.float32)
            e /= max(float(np.linalg.norm(e)), 1e-8)
            found.append({"bbox": (x, y, x + bw, y + bh), "score": float(d[14]), "embedding": e})
        return found


FACE_BACKENDS = {"yunet_sface": YuNetSFaceEngine, "buffalo_l": InsightFaceEngine, "buffalo_s": InsightFaceEngine}


def FaceEngine(pack: str = "yunet_sface", use_gpu: bool = True):
    return FACE_BACKENDS[pack](pack, use_gpu)


def faces_available(pack: str = "yunet_sface") -> bool:
    return all((model_path(f"faces/{pack}") / n).exists() for n in FACE_FILES[pack])
